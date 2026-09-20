"""``fbk journal export`` — the study dataset's export path (docs/12 §8).

Unit (offline): drives the REAL app parser (the exact object the fbk
entrypoint dispatches) against tmp_path roots via ``--root``; journals
are seeded through the real JSONLJournal recorder so every on-disk
entry carries the write-time redaction the export path relies on and
must never weaken (docs/11 §7). Pins the CSV column contract, the
--out overwrite refusal (+ --force opt-in), the exit-1 missing-journal
precondition, torn-trailing tolerance, and that redaction is carried
through the export verbatim — the dataset exports as it lies on disk.
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

import pytest

from app import build_parser
from config import Config
from journal.recorder import JSONLJournal

COLUMNS = ["ts", "method", "status", "content_length", "surface", "url"]


def _clear_env(monkeypatch) -> None:
    monkeypatch.delenv("FBK_ROOT", raising=False)
    monkeypatch.delenv("FBK_COOKIES", raising=False)
    monkeypatch.delenv("FBK_IMPERSONATE", raising=False)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    _clear_env(monkeypatch)


def _run(capsys, argv) -> tuple[int, str, str]:
    """Drive one ``journal export`` invocation; return (rc, stdout, stderr)
    raw — the export body on stdout is CSV or JSON, never an emit payload."""
    args = build_parser().parse_args(argv)
    rc = args.fn(args)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _seed(root: Path, name: str, entries: list[dict[str, Any]]) -> Path:
    """Write one synthetic journal through the real recorder (on-disk bytes
    match a live run, write-time redaction included)."""
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


def _csv_rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


class TestExportCsv:
    """Pins the CSV contract: the fixed six-column header, the flattened
    transport metadata per entry, and blank cells for non-request shapes."""

    def test_csv_round_trip_parses_back(self, tmp_path, capsys):
        entries = [_entry(100.5, surface="feed", status=200),
                   _entry(110.0, surface="search", status=304),
                   _entry(130.0, with_ctx=False),
                   {"ts": 120.0, "event": "session_start"}]
        _seed(tmp_path, "alpha", entries)
        rc, out, _ = _run(
            capsys, ["journal", "export", "--file", "alpha", "--root", str(tmp_path)])
        assert rc == 0
        rows = _csv_rows(out)
        assert rows[0] == COLUMNS
        assert rows[1:] == [
            ["100.5", "GET", "200", "42", "feed", "https://fbk.test/feed"],
            ["110.0", "GET", "304", "42", "search", "https://fbk.test/search"],
            ["130.0", "GET", "200", "42", "", "https://fbk.test/feed"],
            ["120.0", "", "", "", "", ""],
        ]

    def test_empty_journal_exports_header_only(self, tmp_path, capsys):
        path = Config.discover(tmp_path).journal_file("empty")
        path.parent.mkdir(parents=True)
        path.touch()
        rc, out, _ = _run(
            capsys, ["journal", "export", "--file", "empty", "--root", str(tmp_path)])
        assert rc == 0
        assert out == ",".join(COLUMNS) + "\n"


class TestExportJson:
    """Pins the JSON format: the raw entries array, verbatim like
    ``show --json`` — explicit export semantics with --out."""

    def test_json_stdout_is_the_entries_array_verbatim(self, tmp_path, capsys):
        entries = [_entry(100.0), _entry(110.0, surface="search", status=304)]
        _seed(tmp_path, "alpha", entries)
        rc, out, _ = _run(
            capsys, ["journal", "export", "--file", "alpha", "--format", "json",
                     "--root", str(tmp_path)])
        assert rc == 0
        assert json.loads(out) == entries

    def test_json_out_writes_the_array_to_file(self, tmp_path, capsys):
        entries = [_entry(100.0)]
        _seed(tmp_path, "alpha", entries)
        target = tmp_path / "export.json"
        rc, out, _ = _run(
            capsys, ["journal", "export", "--file", "alpha", "--format", "json",
                     "--out", str(target), "--root", str(tmp_path)])
        assert rc == 0
        assert json.loads(target.read_text(encoding="utf-8")) == entries
        assert f"exported 1 entries to {target} (json)" in out


class TestExportOut:
    """Pins the --out semantics: an operator-chosen write target (arbitrary
    path — the point of export), never silently overwriting an existing
    file (refusal + --force opt-in, the house pattern)."""

    def test_out_writes_csv_file_with_confirmation_payload(self, tmp_path, capsys):
        _seed(tmp_path, "alpha", [_entry(100.0), _entry(110.0)])
        target = tmp_path / "alpha.csv"
        rc, out, _ = _run(
            capsys, ["journal", "export", "--file", "alpha", "--out", str(target),
                     "--root", str(tmp_path)])
        assert rc == 0
        assert f"exported 2 entries to {target} (csv)" in out
        # emit contract: the trailing compact JSON line confirms for scripts
        payload = json.loads(out.splitlines()[-1])
        assert payload == {"file": "alpha", "format": "csv",
                           "out": str(target), "rows": 2}
        rows = _csv_rows(target.read_text(encoding="utf-8"))
        assert rows[0] == COLUMNS
        assert len(rows) == 3  # header + 2 data rows

    def test_out_refuses_existing_file_without_force(self, tmp_path, capsys):
        _seed(tmp_path, "alpha", [_entry(100.0)])
        target = tmp_path / "precious.csv"
        target.write_text("KEEP\n", encoding="utf-8")
        rc, _, err = _run(
            capsys, ["journal", "export", "--file", "alpha", "--out", str(target),
                     "--root", str(tmp_path)])
        assert rc == 1
        assert "--force" in err
        assert target.read_text(encoding="utf-8") == "KEEP\n"  # untouched

    def test_out_force_overwrites_existing_file(self, tmp_path, capsys):
        _seed(tmp_path, "alpha", [_entry(100.0)])
        target = tmp_path / "stale.csv"
        target.write_text("OLD\n", encoding="utf-8")
        rc, _, _ = _run(
            capsys, ["journal", "export", "--file", "alpha", "--out", str(target),
                     "--force", "--root", str(tmp_path)])
        assert rc == 0
        rows = _csv_rows(target.read_text(encoding="utf-8"))
        assert rows[0] == COLUMNS  # OLD replaced by the fresh export


class TestExportSemantics:
    """Pins the shared family semantics carried onto export: the exit-1
    missing-journal precondition, the path-shaped name refusal, torn-trailing
    tolerance, and the write-time redaction guarantee carried through."""

    def test_missing_journal_exits_one(self, tmp_path, capsys):
        rc, _, err = _run(
            capsys, ["journal", "export", "--file", "nope", "--root", str(tmp_path)])
        assert rc == 1
        assert "no journal" in err

    @pytest.mark.parametrize("bad", ("../cookies", "sub/dir", "..", "."))
    def test_path_shaped_names_are_refused(self, tmp_path, capsys, bad):
        """--file keeps the family's state/ boundary even on export — only
        --out is an operator-chosen path."""
        rc, _, _ = _run(
            capsys, ["journal", "export", "--file", bad, "--root", str(tmp_path)])
        assert rc == 1

    def test_torn_trailing_line_still_exports(self, tmp_path, capsys):
        path = _seed(tmp_path, "torn", [_entry(100.0), _entry(110.0)])
        _tear(path)
        rc, out, _ = _run(
            capsys, ["journal", "export", "--file", "torn", "--root", str(tmp_path)])
        assert rc == 0
        assert len(_csv_rows(out)) == 3  # header + 2 intact entries

    def test_redaction_is_carried_through_untouched(self, tmp_path, capsys):
        """The entries are redacted AT WRITE (recorder); export adds no
        redaction logic and cannot weaken it — the secret value is nowhere,
        the salted fingerprint is."""
        _seed(tmp_path, "alpha", [dict(_entry(100.0), fb_dtsg="RAW-SECRET-123")])
        rc, out, _ = _run(
            capsys, ["journal", "export", "--file", "alpha", "--format", "json",
                     "--root", str(tmp_path)])
        assert rc == 0
        assert "RAW-SECRET-123" not in out
        assert "<redacted:" in out  # the recorder's salted fingerprint form
        rc, csv_out, _ = _run(
            capsys, ["journal", "export", "--file", "alpha", "--root", str(tmp_path)])
        assert rc == 0
        assert "RAW-SECRET-123" not in csv_out


class TestExportWiring:
    """Pins the subcommand on the real app parser: it parses with a
    handler, --file is required, and --format is constrained to csv/json."""

    def test_export_parses_with_handler(self):
        args = build_parser().parse_args(
            ["journal", "export", "--file", "x", "--out", "y.csv"])
        assert args.journal_command == "export"
        assert callable(args.fn)
        assert args.format == "csv"
        assert args.force is False

    def test_export_requires_file(self, capsys):
        with pytest.raises(SystemExit) as ei:
            build_parser().parse_args(["journal", "export"])
        assert ei.value.code == 2

    def test_export_rejects_unknown_format(self, capsys):
        with pytest.raises(SystemExit) as ei:
            build_parser().parse_args(
                ["journal", "export", "--file", "x", "--format", "xlsx"])
        assert ei.value.code == 2
