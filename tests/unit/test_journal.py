"""Journal discipline tests: secrets never reach disk (docs/11 §7).

Unit (offline): every write lands in a tmp_path JSONL file checked
byte-for-byte — no session, no network.
"""
import json

from journal.recorder import JSONLJournal, redact_entry


class TestJournal:
    """Pins the disk-level secret guarantee: secret-named values redacted,
    non-secret fields visible, input dicts never mutated."""

    def test_secrets_redacted_on_write(self, tmp_path):
        journal = JSONLJournal(tmp_path / "j.jsonl")
        journal.record({
            "event": "test", "xs": "SECRET-XS", "datr": "SECRET-DATR",
            "fb_dtsg": "SECRET-DTSG", "lsd": "SECRET-LSD",
            "friendly_name": "CometPublicQuery", "status": 200,
            "nested": {"c_user": "123", "ok": "visible"},
        })
        disk = (tmp_path / "j.jsonl").read_text()
        assert "SECRET" not in disk
        entry = json.loads(disk)
        assert entry["friendly_name"] == "CometPublicQuery"
        assert entry["nested"]["ok"] == "visible"
        assert entry["xs"].startswith("<redacted:")
        assert "ts" in entry

    def test_read_all_roundtrip(self, tmp_path):
        journal = JSONLJournal(tmp_path / "j.jsonl")
        for i in range(3):
            journal.record({"i": i})
        entries = journal.read_all()
        assert [e["i"] for e in entries] == [0, 1, 2]

    def test_redact_entry_does_not_mutate_input(self):
        source = {"xs": "keep-me"}
        redact_entry(source)
        assert source["xs"] == "keep-me"
