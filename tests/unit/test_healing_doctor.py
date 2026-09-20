"""The doctor's "self-healing" check + ``HealingLog.last_event``.

Unit (offline): the same tmp_path-rooted register() seam harness as
test_doctor.py (house pattern) drives the REAL check against a synthetic
root; the healing log is seeded through healing.HealingLog itself (the
append path doctor reads back via count_since/last_event), never
hand-written JSON — except the corrupt-log case, which must be raw
garbage by construction. Verdict contract under test: PASS when healing
is enabled and the log is absent or parseable (a disabled FBK_HEAL is a
legitimate choice, still a pass); WARN when the file is non-empty but
yields zero parseable rows; FAIL never.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import commands.doctor as doctor_mod
from commands.common import run_command
from commands.doctor import register
from healing import (
    KIND_DOC_ID_RETRY,
    KIND_REGISTRY_REFRESH,
    KIND_TOKEN_CACHE_REBUILD,
    KIND_TRANSPORT_RETRY,
    HealingLog,
)


def _parse(argv: list[str]) -> argparse.Namespace:
    """Parse argv against doctor's own register() seam (test_doctor.py)."""
    parser = argparse.ArgumentParser(prog="fbk", allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    register(sub)
    return parser.parse_args(argv)


def _run(capsys, argv: list[str]) -> tuple[int, str, dict]:
    """Drive ``doctor`` through the run_command barrier; return (rc, stdout,
    payload) — whole-output JSON in --json mode, trailing compact line in
    human mode (the emit contract; [heal] stderr mirrors never land here)."""
    args = _parse(argv)
    rc = run_command(args.fn, args)
    out = capsys.readouterr().out
    try:
        return rc, out, json.loads(out)
    except json.JSONDecodeError:
        return rc, out, json.loads(out.splitlines()[-1])


def _healthy_env(root: Path) -> None:
    """A fully healthy offline env (test_doctor.py pattern): both registry
    tiers, every referenced capture asset, a writable state/ dir."""
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    reg = json.dumps({"revision": "rev-test",
                      "unique_pairs": [{"friendly_name": "A", "doc_id": "1"},
                                       {"friendly_name": "B", "doc_id": "2"}]})
    (data / "doc_id_registry_v3.json").write_text(reg, encoding="utf-8")
    (data / "doc_id_registry_v2.json").write_text(reg, encoding="utf-8")
    cap = json.dumps({"mutations": [{"friendly_name": "M", "variables": {}}]})
    for name in ("captured_mutations.json", "captured_comment_mutations.json",
                 "captured_composer.json"):
        (data / name).write_text(cap, encoding="utf-8")
    (root / "state").mkdir(exist_ok=True)


def _stub_probe(monkeypatch, result: str = "chrome136") -> None:
    """Stub the impersonate resolution (local probe, but slow to import)."""
    monkeypatch.setattr(doctor_mod, "resolve_impersonate",
                        lambda preferred: result)


def _check(payload: dict, name: str) -> dict:
    """One check's dict by name (test_doctor.py pattern)."""
    return next(c for c in payload["checks"] if c["name"] == name)


def _human_line(out: str) -> str:
    """The check's human status line (never the trailing compact JSON)."""
    return next(line for line in out.splitlines()
                if line.startswith("[") and "self-healing" in line)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("FBK_ROOT", raising=False)
    monkeypatch.delenv("FBK_COOKIES", raising=False)
    monkeypatch.delenv("FBK_IMPERSONATE", raising=False)
    monkeypatch.delenv("FBK_HEAL", raising=False)  # ambient switch pinned per-test


class TestSelfHealingCheck:
    """The doctor's self-healing check: verdict contract + payload readout."""

    def test_no_log_passes_with_empty_breakdown(
            self, tmp_path, monkeypatch, capsys):
        """(a) no log file: enabled, pass, zeroed breakdown, last None."""
        _healthy_env(tmp_path)
        _stub_probe(monkeypatch)
        rc, _, payload = _run(capsys, ["doctor", "--root", str(tmp_path),
                                       "--json"])
        assert rc == 0
        readout = payload["self_healing"]
        assert readout["enabled"] is True
        assert readout["log"] == str(
            tmp_path.resolve() / "state" / "healing.jsonl")
        assert readout["events_24h"] == 0
        assert readout["by_kind"] == {KIND_REGISTRY_REFRESH: 0,
                                      KIND_DOC_ID_RETRY: 0,
                                      KIND_TOKEN_CACHE_REBUILD: 0,
                                      KIND_TRANSPORT_RETRY: 0}
        assert readout["last"] is None
        check = _check(payload, "self-healing")
        assert check["status"] == "pass"
        assert check["critical"] is False
        assert "no log yet" in check["detail"]
        assert check["hint"] is None

    def test_seeded_log_counts_and_last_event_json(
            self, tmp_path, monkeypatch, capsys):
        """(b) seeded log (--json): 24h count, per-kind breakdown, and the
        newest row render through the payload readout."""
        _healthy_env(tmp_path)
        _stub_probe(monkeypatch)
        now = time.time()
        log = HealingLog(tmp_path / "state" / "healing.jsonl")
        log.append(KIND_REGISTRY_REFRESH, "doc_id registry re-harvested",
                   "added=3 changed=1 bundles=12 errors=0", ts=now - 7200.0)
        log.append(KIND_DOC_ID_RETRY, "1570245-family rejection",
                   "retry on the fresh doc_id", ts=now - 3600.0)
        rc, _, payload = _run(capsys, ["doctor", "--root", str(tmp_path),
                                       "--json"])
        assert rc == 0
        readout = payload["self_healing"]
        assert readout["events_24h"] == 2
        assert readout["by_kind"] == {KIND_REGISTRY_REFRESH: 1,
                                      KIND_DOC_ID_RETRY: 1,
                                      KIND_TOKEN_CACHE_REBUILD: 0,
                                      KIND_TRANSPORT_RETRY: 0}
        assert readout["last"] == {"ts": now - 3600.0,
                                   "kind": KIND_DOC_ID_RETRY,
                                   "trigger": "1570245-family rejection",
                                   "detail": "retry on the fresh doc_id"}
        check = _check(payload, "self-healing")
        assert check["status"] == "pass"
        assert "2 event(s) in 24h" in check["detail"]

    def test_seeded_log_human_mode_renders_last_summary(
            self, tmp_path, monkeypatch, capsys):
        """(b) seeded log (human): the [PASS] line carries the last-event
        kind+trigger summary; the trailing compact line carries the readout."""
        _healthy_env(tmp_path)
        _stub_probe(monkeypatch)
        log = HealingLog(tmp_path / "state" / "healing.jsonl")
        log.append(KIND_TOKEN_CACHE_REBUILD, "corrupt token cache",
                   "cache discarded; fresh bootstrap regenerated tokens",
                   ts=time.time() - 60.0)
        rc, out, payload = _run(capsys, ["doctor", "--root", str(tmp_path)])
        assert rc == 0
        check = _check(payload, "self-healing")
        assert check["status"] == "pass"
        assert "1 event(s) in 24h" in check["detail"]
        assert (f"last: {KIND_TOKEN_CACHE_REBUILD}: corrupt token cache"
                in check["detail"])
        line = _human_line(out)
        assert line.startswith("[PASS]")
        assert "healing on" in line
        tail = json.loads(out.splitlines()[-1])
        assert tail["self_healing"]["last"]["trigger"] == "corrupt token cache"

    def test_corrupt_non_json_log_warns_never_fails(
            self, tmp_path, monkeypatch, capsys):
        """(c) a non-empty log of pure garbage: WARN, exit still 0, readout
        zeroes out (healing problems must not fail the doctor)."""
        _healthy_env(tmp_path)
        _stub_probe(monkeypatch)
        (tmp_path / "state" / "healing.jsonl").write_text(
            "{ not json\n{torn tail", encoding="utf-8")
        rc, _, payload = _run(capsys, ["doctor", "--root", str(tmp_path),
                                       "--json"])
        assert rc == 0
        check = _check(payload, "self-healing")
        assert check["status"] == "warn"
        assert check["hint"] is not None
        assert "unreadable" in check["detail"]
        readout = payload["self_healing"]
        assert readout["events_24h"] == 0
        assert readout["last"] is None

    def test_corrupt_log_human_line_warns(self, tmp_path, monkeypatch, capsys):
        """(c) human mode: the WARN line + remediation hint render."""
        _healthy_env(tmp_path)
        _stub_probe(monkeypatch)
        (tmp_path / "state" / "healing.jsonl").write_text(
            "garbage", encoding="utf-8")
        rc, out, _ = _run(capsys, ["doctor", "--root", str(tmp_path)])
        assert rc == 0
        assert _human_line(out).startswith("[WARN]")
        assert "hint:" in out

    def test_partial_corruption_with_parseable_rows_still_passes(
            self, tmp_path, monkeypatch, capsys):
        """A torn TAIL among parseable rows stays a pass (fail-soft prefix
        read by design, healing.py) — WARN is only for zero parseable rows."""
        _healthy_env(tmp_path)
        _stub_probe(monkeypatch)
        log = HealingLog(tmp_path / "state" / "healing.jsonl")
        log.append(KIND_TRANSPORT_RETRY, "connect-phase blip", "retried once",
                   ts=time.time() - 30.0)
        with log.path.open("a", encoding="utf-8") as fh:
            fh.write("{ torn tail\n")
        rc, _, payload = _run(capsys, ["doctor", "--root", str(tmp_path),
                                       "--json"])
        assert rc == 0
        check = _check(payload, "self-healing")
        assert check["status"] == "pass"
        assert payload["self_healing"]["events_24h"] == 1

    def test_fbk_heal_off_reports_disabled(self, tmp_path, monkeypatch, capsys):
        """(d) FBK_HEAL=off (ambient env): enabled False, still a pass —
        a disabled switch is a legitimate choice, never a failure; the
        existing log is still censused for visibility."""
        _healthy_env(tmp_path)
        _stub_probe(monkeypatch)
        monkeypatch.setenv("FBK_HEAL", "off")
        log = HealingLog(tmp_path / "state" / "healing.jsonl")
        log.append(KIND_TRANSPORT_RETRY, "connect-phase blip", "retried once",
                   ts=time.time() - 30.0)
        rc, _, payload = _run(capsys, ["doctor", "--root", str(tmp_path),
                                       "--json"])
        assert rc == 0
        readout = payload["self_healing"]
        assert readout["enabled"] is False
        assert readout["events_24h"] == 1
        check = _check(payload, "self-healing")
        assert check["status"] == "pass"
        assert "healing off" in check["detail"]
        assert "FBK_HEAL" in check["detail"]


class TestHealingLogLastEvent:
    """HealingLog.last_event() — the tiny public reader added for the
    doctor: the newest parseable row wins; absent/empty/fully-corrupt
    logs yield None (the doctor's torn-log signal)."""

    def test_last_parseable_row_wins(self, tmp_path):
        log = HealingLog(tmp_path / "healing.jsonl")
        now = time.time()
        log.append(KIND_REGISTRY_REFRESH, "first", ts=now - 100.0)
        log.append(KIND_TRANSPORT_RETRY, "second", ts=now)
        assert log.last_event() == {"ts": now, "kind": KIND_TRANSPORT_RETRY,
                                    "trigger": "second", "detail": ""}

    def test_absent_and_empty_logs_yield_none(self, tmp_path):
        absent = HealingLog(tmp_path / "missing.jsonl")
        assert absent.last_event() is None
        empty = HealingLog(tmp_path / "empty.jsonl")
        empty.path.touch()
        assert empty.last_event() is None

    def test_corrupt_lines_are_skipped(self, tmp_path):
        path = tmp_path / "healing.jsonl"
        path.write_text("{ garbage lead\n", encoding="utf-8")
        log = HealingLog(path)
        log.append(KIND_DOC_ID_RETRY, "kept row", ts=123.0)
        with path.open("a", encoding="utf-8") as fh:
            fh.write("[ torn tail\n")
        last = log.last_event()
        assert last is not None
        assert last["kind"] == KIND_DOC_ID_RETRY
        assert last["trigger"] == "kept row"

    def test_fully_corrupt_log_yields_none(self, tmp_path):
        path = tmp_path / "healing.jsonl"
        path.write_text("{ not json\n[", encoding="utf-8")
        assert HealingLog(path).last_event() is None
