"""Journal recorder tests: session_start, ts preservation, blank-line
tolerance, iter_journals ordering, and full token-name redaction.

Complements test_journal.py (secret cookie names, roundtrip, input
immutability). Redaction here is the hard guarantee of docs/11 §7:
EVERY secret-named value (cookies AND tokens) is fingerprinted before
it can reach disk.
"""
from __future__ import annotations

import json

import pytest

import constants as C
from journal.recorder import JSONLJournal, iter_journals, redact_entry
from transport.cookies import fingerprint, redact


class TestRecordShapes:
    """Pins record/session_start entry shapes and explicit-ts preservation."""

    def test_session_start_adds_event_field(self, tmp_path):
        journal = JSONLJournal(tmp_path / "j.jsonl")
        journal.session_start({"user_id": "123", "friendly_name": "boot"})
        entries = journal.read_all()
        assert len(entries) == 1
        assert entries[0]["event"] == "session_start"
        assert entries[0]["user_id"] == "123"
        assert entries[0]["friendly_name"] == "boot"

    def test_explicit_ts_is_never_overwritten(self, tmp_path):
        journal = JSONLJournal(tmp_path / "j.jsonl")
        journal.record({"ts": 42.5, "x": 1})
        assert journal.read_all()[0]["ts"] == 42.5

    def test_record_appends_lines(self, tmp_path):
        journal = JSONLJournal(tmp_path / "j.jsonl")
        for i in range(3):
            journal.record({"i": i})
        lines = (tmp_path / "j.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3

    def test_read_all_skips_blank_lines(self, tmp_path):
        path = tmp_path / "j.jsonl"
        path.write_text('{"a": 1}\n\n{"b": 2}\n', encoding="utf-8")
        entries = JSONLJournal(path).read_all()
        assert entries == [{"a": 1}, {"b": 2}]

    def test_read_all_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            JSONLJournal(tmp_path / "never.jsonl").read_all()


class TestIterJournals:
    """Pins journal discovery: oldest-first name ordering and the
    *.jsonl-only filter."""

    def test_sorted_oldest_name_first(self, tmp_path):
        for name in ("c.jsonl", "a.jsonl", "b.jsonl"):
            (tmp_path / name).write_text("{}\n", encoding="utf-8")
        assert [p.name for p in iter_journals(tmp_path)] == \
            ["a.jsonl", "b.jsonl", "c.jsonl"]

    def test_missing_directory_yields_nothing(self, tmp_path):
        assert list(iter_journals(tmp_path / "absent")) == []

    def test_only_jsonl_files_match(self, tmp_path):
        (tmp_path / "keep.jsonl").write_text("{}\n", encoding="utf-8")
        (tmp_path / "ignore.txt").write_text("x", encoding="utf-8")
        (tmp_path / "ignore.json").write_text("{}", encoding="utf-8")
        assert [p.name for p in iter_journals(tmp_path)] == ["keep.jsonl"]


class TestTokenNameRedaction:
    """Pins the docs/11 §7 guarantee: every secret-named value (cookie AND
    token families) is fingerprinted before it can reach disk."""

    def test_every_secret_token_name_is_fingerprinted(self):
        entry = {name: f"VALUE-{name}" for name in C.SECRET_TOKEN_NAMES}
        entry["keep"] = "visible"
        out = redact_entry(entry)
        assert out["keep"] == "visible"
        for name in C.SECRET_TOKEN_NAMES:
            assert out[name].startswith("<redacted:")
            assert f"VALUE-{name}" not in out[name]

    def test_redacted_value_is_the_salted_fingerprint(self):
        out = redact({"fb_dtsg": "NAfTEST:1:1789723298"},
                     C.SECRET_TOKEN_NAMES)
        assert out["fb_dtsg"] == f"<redacted:{fingerprint('NAfTEST:1:1789723298')}>"

    def test_secrets_redacted_inside_nested_lists(self):
        out = redact(
            {"xs": "S1", "frames": [{"lsd": "S2", "ok": 1}, ["xs", "name"]]},
            C.SECRET_COOKIE_NAMES | C.SECRET_TOKEN_NAMES)
        assert "S1" not in json.dumps(out)
        assert out["frames"][0]["ok"] == 1
        assert out["frames"][0]["lsd"].startswith("<redacted:")

    def test_journal_secret_names_union_covers_both_families(self, tmp_path):
        import journal.recorder as recorder
        assert recorder.ALL_SECRETS >= C.SECRET_COOKIE_NAMES
        assert recorder.ALL_SECRETS >= C.SECRET_TOKEN_NAMES
