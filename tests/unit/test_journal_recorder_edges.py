"""journal.recorder edge coverage (wave 2 polish targets).

test_journal_recorder.py + test_journal.py cover shapes, ts handling,
ordering and redaction. What was missing:

  * non-JSON-native values must still journal (default=str), never raise;
  * non-ASCII payload characters survive on disk (ensure_ascii=False) —
    the multilingual feed text is first-class journal data;
  * nested journal directories are created eagerly;
  * iter_journals over a FILE path yields nothing (no TypeError);
  * a corrupt line fails loudly on read (journals are fail-soft on WRITE,
    but read_all is the analysis tool and must not silently drop data);
  * read_all(strict=False) tolerates exactly one shape of damage: a
    crash-torn TRAILING line (the no-fsync design's documented failure
    mode) — skipped with a stderr note; interior lines stay loud;
  * journal instances sharing one path append (never truncate).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from journal.recorder import JSONLJournal, iter_journals


class TestValueCoercion:
    """Pins non-JSON-native coercion (default=str) and byte-for-byte
    survival of non-ASCII payload text on disk."""

    def test_non_json_native_values_are_coerced_never_raised(self, tmp_path):
        journal = JSONLJournal(tmp_path / "j.jsonl")
        journal.record({"path": Path("/tmp/x"), "tags": {"a", "b"},
                        "note": ("tuple", "shape")})
        entry = journal.read_all()[0]
        assert "tmp" in str(entry["path"])
        assert isinstance(entry["tags"], str)

    def test_multilingual_text_survives_byte_for_byte(self, tmp_path):
        journal = JSONLJournal(tmp_path / "j.jsonl")
        journal.record({"text": "ঢাকা — Ünïcødé ✓"})
        disk = (tmp_path / "j.jsonl").read_text(encoding="utf-8")
        assert "ঢাকা" in disk            # the characters themselves…
        assert "\\u0982" not in disk      # …not ASCII-escaped away
        assert json.loads(disk.splitlines()[0])["text"].startswith("ঢাকা")


class TestPathHandling:
    """Pins eager nested-directory creation, iter_journals tolerance of a
    FILE argument, and append-only sharing of one path."""

    def test_nested_directories_are_created(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c" / "j.jsonl"
        journal = JSONLJournal(deep)
        assert deep.parent.is_dir()
        journal.record({"i": 1})
        assert deep.is_file()

    def test_iter_journals_over_a_file_path_yields_nothing(self, tmp_path):
        a_file = tmp_path / "plain.jsonl"
        a_file.write_text("{}\n", encoding="utf-8")
        assert list(iter_journals(a_file)) == []

    def test_instances_sharing_a_path_append_never_truncate(self, tmp_path):
        path = tmp_path / "shared.jsonl"
        JSONLJournal(path).record({"who": "first"})
        JSONLJournal(path).record({"who": "second"})
        entries = JSONLJournal(path).read_all()
        assert [e["who"] for e in entries] == ["first", "second"]


class TestCorruptReads:
    """Pins read-side loudness: corrupt lines fail read_all (the analysis
    tool must not silently drop data); ts-less hand-written lines parse."""

    def test_corrupt_line_fails_loudly_on_read(self, tmp_path):
        path = tmp_path / "j.jsonl"
        path.write_text('{"ok": 1}\nnot-json-at-all\n', encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            JSONLJournal(path).read_all()

    def test_valid_lines_around_a_missing_ts_still_parse(self, tmp_path):
        """Entries without a ts (hand-written analysis files) read back."""
        path = tmp_path / "j.jsonl"
        path.write_text('{"n": 1}\n{"n": 2}\n', encoding="utf-8")
        assert [e["n"] for e in JSONLJournal(path).read_all()] == [1, 2]


class TestTornTrailingLine:
    """Pins the strict=False read mode: a crash-torn TRAILING line is the
    no-fsync design's one documented damage shape and may be skipped
    with a stderr note; interior corrupt lines stay loud in BOTH modes
    (they are disk rot or tampering, never a torn append)."""

    def _torn_file(self, tmp_path):
        path = tmp_path / "j.jsonl"
        # torn: the crash cut the write before the closing brace + newline
        path.write_text('{"n": 1}\n{"n": 2}\n{"n": 3', encoding="utf-8")
        return path

    def test_torn_trailing_line_skipped_with_note_when_not_strict(
            self, tmp_path, capsys):
        entries = JSONLJournal(self._torn_file(tmp_path)).read_all(strict=False)
        assert [e["n"] for e in entries] == [1, 2]
        assert "torn" in capsys.readouterr().err

    def test_torn_trailing_line_stays_loud_when_strict(self, tmp_path):
        with pytest.raises(json.JSONDecodeError):
            JSONLJournal(self._torn_file(tmp_path)).read_all(strict=True)

    def test_torn_trailing_line_loud_by_default(self, tmp_path):
        with pytest.raises(json.JSONDecodeError):
            JSONLJournal(self._torn_file(tmp_path)).read_all()

    def test_interior_corrupt_line_loud_even_when_not_strict(self, tmp_path):
        path = tmp_path / "j.jsonl"
        path.write_text('{"n": 1}\nnot-json\n{"n": 2}\n', encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            JSONLJournal(path).read_all(strict=False)
