"""``fbk journal list/show/stats`` — offline review of the study dataset.

Unit (offline): drives the REAL app parser (the exact object the fbk
entrypoint dispatches) against tmp_path roots via ``--root`` (the
CLI-flag equivalent of FBK_ROOT); journals are seeded through the real
JSONLJournal recorder so every on-disk entry carries the write-time
redaction the display path relies on (docs/12 §8 — the journals ARE
the dataset; docs/11 §7 — redaction happened before the write).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app import build_parser
from config import Config
from journal.recorder import JSONLJournal


def _clear_env(monkeypatch) -> None:
    monkeypatch.delenv("FBK_ROOT", raising=False)
    monkeypatch.delenv("FBK_COOKIES", raising=False)
    monkeypatch.delenv("FBK_IMPERSONATE", raising=False)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    _clear_env(monkeypatch)


def _run(capsys, argv) -> tuple[int, str, dict]:
    """Drive one ``journal`` subcommand end to end; return (rc, stdout,
    payload) — the payload parses from the WHOLE output in --json mode
    and from the trailing compact line in human mode (the emit contract)."""
    args = build_parser().parse_args(argv)
    rc = args.fn(args)
    out = capsys.readouterr().out
    if not out.strip():
        # the exit-1 precondition path: stderr note only, no payload
        return rc, out, {}
    try:
        return rc, out, json.loads(out)
    except json.JSONDecodeError:
        return rc, out, json.loads(out.splitlines()[-1])


def _seed(root: Path, name: str, entries: list[dict[str, Any]]) -> Path:
    """Write one synthetic journal through the real recorder (so the
    on-disk bytes match what a live run produces, redaction included)."""
    path = Config.discover(root).journal_file(name)
    journal = JSONLJournal(path)
    for entry in entries:
        journal.record(entry)
    return path


def _entry(ts: float, surface: str = "feed", status: int = 200,
           with_ctx: bool = True) -> dict[str, Any]:
    """One transport-shaped journal entry (the _note shape: ts, method,
    url, status, content_length, ctx.surface)."""
    entry: dict[str, Any] = {"ts": ts, "method": "GET",
                             "url": f"https://fbk.test/{surface}",
                             "status": status, "content_length": 42}
    if with_ctx:
        entry["ctx"] = {"surface": surface}
    return entry


def _tear(path: Path) -> None:
    """Crash-tear the trailing line: no closing brace, no newline — the
    no-fsync design's one documented damage shape."""
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"ts": 999.0, "method": "GET"')


class TestJournalList:
    """Pins the list payload on a synthetic state/: multi-file facts,
    the zero-journal report, and torn-trailing tolerance."""

    def test_multi_file_listing_reports_each_journal(self, tmp_path, capsys):
        _seed(tmp_path, "beta", [_entry(200.0)])
        _seed(tmp_path, "alpha", [_entry(100.0), _entry(110.0)])
        rc, out, payload = _run(capsys, ["journal", "list", "--root", str(tmp_path)])
        assert rc == 0
        assert payload["count"] == 2
        # iter_journals order: lexicographic (datestamped names → chronological)
        assert [j["name"] for j in payload["journals"]] == ["alpha", "beta"]
        alpha = payload["journals"][0]
        assert alpha["entries"] == 2
        assert alpha["first_ts"] == 100.0
        assert alpha["last_ts"] == 110.0
        assert alpha["size_bytes"] > 0
        assert alpha["torn_trailing_line"] is False
        assert "alpha" in out and "beta" in out

    def test_missing_state_dir_is_zero_journals_exit_zero(self, tmp_path, capsys):
        """The state dir may not even exist yet — absence is payload data,
        never an error (same contract as config show's missing jar)."""
        assert not (tmp_path / "state").exists()
        rc, out, payload = _run(capsys, ["journal", "list", "--root", str(tmp_path)])
        assert rc == 0
        assert payload["count"] == 0
        assert payload["journals"] == []
        assert "no journals" in out

    def test_present_but_empty_state_dir_is_zero_journals(self, tmp_path, capsys):
        (tmp_path / "state").mkdir()
        rc, out, payload = _run(capsys, ["journal", "list", "--root", str(tmp_path)])
        assert rc == 0
        assert payload["count"] == 0
        assert "no journals" in out

    def test_torn_trailing_line_still_lists(self, tmp_path, capsys):
        path = _seed(tmp_path, "torn", [_entry(100.0), _entry(110.0)])
        _tear(path)
        rc, _, payload = _run(capsys, ["journal", "list", "--root", str(tmp_path)])
        assert rc == 0
        [journal] = payload["journals"]
        assert journal["entries"] == 2  # the torn line skipped, not fatal
        assert journal["first_ts"] == 100.0
        assert journal["last_ts"] == 110.0
        assert journal["torn_trailing_line"] is True


class TestJournalShow:
    """Pins the show window (head default 20 / --limit / --tail), the
    verbatim display of write-time-redacted entries, and the exit-1
    precondition for a missing journal or a path-shaped --file."""

    def test_show_defaults_to_first_twenty(self, tmp_path, capsys):
        _seed(tmp_path, "big", [_entry(float(i)) for i in range(25)])
        rc, _, payload = _run(
            capsys, ["journal", "show", "--file", "big", "--root", str(tmp_path)])
        assert rc == 0
        assert payload["total"] == 25
        assert payload["shown"] == 20
        assert [e["ts"] for e in payload["entries"]] == [float(i) for i in range(20)]

    def test_show_limit_head(self, tmp_path, capsys):
        _seed(tmp_path, "alpha", [_entry(float(i)) for i in range(5)])
        rc, _, payload = _run(
            capsys, ["journal", "show", "--file", "alpha", "--limit", "3",
                     "--root", str(tmp_path)])
        assert rc == 0
        assert [e["ts"] for e in payload["entries"]] == [0.0, 1.0, 2.0]

    def test_show_tail_takes_the_last_window(self, tmp_path, capsys):
        _seed(tmp_path, "alpha", [_entry(float(i)) for i in range(5)])
        rc, _, payload = _run(
            capsys, ["journal", "show", "--file", "alpha", "--limit", "3",
                     "--tail", "--root", str(tmp_path)])
        assert rc == 0
        assert [e["ts"] for e in payload["entries"]] == [2.0, 3.0, 4.0]

    def test_show_json_emits_raw_entries_verbatim(self, tmp_path, capsys):
        entries = [_entry(100.0), _entry(110.0, surface="search", status=304)]
        _seed(tmp_path, "alpha", entries)
        rc, out, payload = _run(
            capsys, ["journal", "show", "--file", "alpha", "--root", str(tmp_path),
                     "--json"])
        assert rc == 0
        # verbatim: display adds nothing, write-time redaction already applied
        assert payload["entries"] == entries
        assert json.loads(out) == payload  # --json mode is machine-only
        assert "journal alpha" not in out  # no human decoration

    def test_show_human_line_is_ts_method_status_url(self, tmp_path, capsys):
        _seed(tmp_path, "alpha", [_entry(100.0)])
        rc, out, _ = _run(
            capsys, ["journal", "show", "--file", "alpha", "--root", str(tmp_path)])
        assert rc == 0
        assert "GET" in out and "200" in out and "https://fbk.test/feed" in out

    def test_show_tolerates_torn_trailing_line(self, tmp_path, capsys):
        path = _seed(tmp_path, "torn", [_entry(100.0), _entry(110.0)])
        _tear(path)
        rc, _, payload = _run(
            capsys, ["journal", "show", "--file", "torn", "--root", str(tmp_path)])
        assert rc == 0
        assert payload["total"] == 2
        assert payload["shown"] == 2

    def test_show_missing_file_exits_one(self, tmp_path, capsys):
        rc, _, _ = _run(
            capsys, ["journal", "show", "--file", "nope", "--root", str(tmp_path)])
        assert rc == 1

    @pytest.mark.parametrize("bad", ("../cookies", "sub/dir", "..\\evil", "..", "."))
    def test_show_rejects_path_shaped_names(self, tmp_path, capsys, bad):
        """The security boundary: --file is a bare NAME resolved through
        Config.journal_file — path-shaped values exit 1 and never touch
        anything outside state/."""
        rc, _, _ = _run(
            capsys, ["journal", "show", "--file", bad, "--root", str(tmp_path)])
        assert rc == 1
        assert not (tmp_path.parent / "cookies.jsonl").exists()


class TestJournalStats:
    """Pins the stats aggregation: surface counts, status distribution,
    exact gap math on a known ts sequence (percentile is nearest-rank),
    and the exit-1 precondition for a missing journal."""

    def test_stats_aggregates_known_sequence(self, tmp_path, capsys):
        _seed(tmp_path, "alpha", [
            _entry(100.0, surface="feed", status=200),
            _entry(110.0, surface="feed", status=200),
            _entry(130.0, surface="feed", status=304),
            _entry(170.0, surface="search", status=200),
        ])
        rc, out, payload = _run(
            capsys, ["journal", "stats", "--file", "alpha", "--root", str(tmp_path)])
        assert rc == 0
        assert payload["total"] == 4
        assert payload["surfaces"] == {"feed": 3, "search": 1}
        assert payload["status_codes"] == {"200": 3, "304": 1}
        # gaps over (100,110,130,170): [10, 20, 40]; nearest-rank p50=20, p95=40
        assert payload["gap_seconds"] == {"count": 3, "min_s": 10.0,
                                          "p50_s": 20.0, "p95_s": 40.0,
                                          "max_s": 40.0}
        assert payload["span_seconds"] == 70.0
        assert payload["first_ts"] == 100.0
        assert payload["last_ts"] == 170.0
        assert "feed" in out and "200" in out  # human blocks render

    def test_stats_counts_surfaceless_entries_as_none(self, tmp_path, capsys):
        _seed(tmp_path, "bare", [_entry(100.0, with_ctx=False),
                                 _entry(110.0, surface="feed")])
        rc, _, payload = _run(
            capsys, ["journal", "stats", "--file", "bare", "--root", str(tmp_path)])
        assert rc == 0
        assert payload["surfaces"] == {"(none)": 1, "feed": 1}

    def test_stats_single_entry_has_empty_gaps(self, tmp_path, capsys):
        _seed(tmp_path, "one", [_entry(100.0)])
        rc, _, payload = _run(
            capsys, ["journal", "stats", "--file", "one", "--root", str(tmp_path)])
        assert rc == 0
        assert payload["gap_seconds"]["count"] == 0
        assert payload["gap_seconds"]["p50_s"] == 0.0
        assert payload["gap_seconds"]["p95_s"] == 0.0
        assert payload["gap_seconds"]["min_s"] is None
        assert payload["gap_seconds"]["max_s"] is None
        assert payload["span_seconds"] == 0.0

    def test_stats_tolerates_torn_trailing_line(self, tmp_path, capsys):
        path = _seed(tmp_path, "torn", [_entry(100.0), _entry(110.0)])
        _tear(path)
        rc, _, payload = _run(
            capsys, ["journal", "stats", "--file", "torn", "--root", str(tmp_path)])
        assert rc == 0
        assert payload["total"] == 2
        assert payload["gap_seconds"]["count"] == 1

    def test_stats_missing_file_exits_one(self, tmp_path, capsys):
        rc, _, _ = _run(
            capsys, ["journal", "stats", "--file", "nope", "--root", str(tmp_path)])
        assert rc == 1


class TestJournalWiring:
    """Pins the family on the real app parser: children parse with
    handlers, the family requires a child, show requires --file, and
    the top-level help advertises the family."""

    @pytest.mark.parametrize("argv,child", (
        (["journal", "list"], "list"),
        (["journal", "show", "--file", "x"], "show"),
        (["journal", "stats", "--file", "x"], "stats"),
    ))
    def test_family_parses_with_children(self, argv, child):
        args = build_parser().parse_args(argv)
        assert callable(args.fn)
        assert args.journal_command == child

    def test_family_requires_a_child(self, capsys):
        with pytest.raises(SystemExit) as ei:
            build_parser().parse_args(["journal"])
        assert ei.value.code == 2
        assert "required" in capsys.readouterr().err

    def test_show_requires_file(self, capsys):
        with pytest.raises(SystemExit) as ei:
            build_parser().parse_args(["journal", "show"])
        assert ei.value.code == 2

    def test_family_visible_in_top_level_help(self, capsys):
        build_parser().print_help()
        assert "journal" in capsys.readouterr().out
