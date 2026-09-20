"""Unit tests for `fbk doctor --fix` — the offline quarantine repair pass.

All tests execute offline (no network, no cookies). The --fix pass
renames corrupt state files aside (never deletes) and records every
quarantine in the healing log. Covers: corrupt governor state, corrupt
token cache, torn healing log, the no-op pass over a healthy
environment, the human-mode [FIXED] rendering, and the healing-log
event trail each quarantine leaves.

Harness mirrors tests/unit/test_doctor.py (the register() seam +
run_command barrier) — the doctor module's own contract surface.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from commands.common import run_command
from commands.doctor import register
from healing import (
    KIND_DOCTOR_FIX,
    KIND_GOVERNOR_STATE_REBUILD,
    KIND_TOKEN_CACHE_REBUILD,
    HealingLog,
)


def _parse(argv: list[str]) -> argparse.Namespace:
    """Parse argv against doctor's own register() seam."""
    parser = argparse.ArgumentParser(prog="fbk", allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    register(sub)
    return parser.parse_args(argv)


def _run(capsys, argv: list[str]) -> tuple[int, dict]:
    """Drive doctor through run_command; parse the payload (--json mode)."""
    args = _parse(argv)
    rc = run_command(args.fn, args)
    out = capsys.readouterr().out
    return rc, json.loads(out)


def _healthy_env(root: Path) -> None:
    """The minimal offline env the doctor's other checks accept."""
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


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """Isolate the ambient env (the fix pass reads FBK_HEAL nowhere, but
    the governor/kind wiring must stay deterministic)."""
    for name in ("FBK_ROOT", "FBK_COOKIES", "FBK_IMPERSONATE", "FBK_HEAL"):
        monkeypatch.delenv(name, raising=False)


def _corrupt(root: Path, name: str) -> None:
    (root / "state" / name).write_text("{torn", encoding="utf-8")


class TestDoctorFix:
    """The offline quarantine pass."""

    def test_fix_quarantines_corrupt_governor_and_cache(
            self, tmp_path, capsys):
        _healthy_env(tmp_path)
        _corrupt(tmp_path, "governor_state.json")
        _corrupt(tmp_path, "token_cache.json")
        rc, payload = _run(capsys, ["doctor", "--root", str(tmp_path),
                                    "--json", "--fix"])
        assert rc == 0
        fixed = payload["fixed"]
        assert {e["file"] for e in fixed} == {"governor_state.json",
                                              "token_cache.json"}
        state = tmp_path / "state"
        for entry in fixed:
            assert not (state / entry["file"]).exists()
            quarantined = state / entry["quarantined_to"]
            assert quarantined.is_file()  # evidence kept, never deleted
            assert ".corrupt-" in entry["quarantined_to"]
        # every quarantine is logged into the (fresh) healing log
        rows = [json.loads(line) for line in
                (state / "healing.jsonl").read_text(
                    encoding="utf-8").splitlines()]
        kinds = {row["kind"] for row in rows}
        assert kinds == {KIND_GOVERNOR_STATE_REBUILD, KIND_TOKEN_CACHE_REBUILD}
        assert all("--fix" in row["detail"] for row in rows)

    def test_fix_renames_aside_not_deletes(self, tmp_path, capsys):
        _healthy_env(tmp_path)
        _corrupt(tmp_path, "governor_state.json")
        _run(capsys, ["doctor", "--root", str(tmp_path), "--json", "--fix"])
        leftovers = [p.name for p in (tmp_path / "state").iterdir()]
        assert any("governor_state.json.corrupt-" in n for n in leftovers)
        # the corrupt bytes survive in the quarantine copy
        quarantined = next(p for p in (tmp_path / "state").iterdir()
                           if p.name.startswith("governor_state.json.corrupt"))
        assert quarantined.read_text(encoding="utf-8") == "{torn"

    def test_fix_repairs_a_torn_healing_log(self, tmp_path, capsys):
        _healthy_env(tmp_path)
        (tmp_path / "state" / "healing.jsonl").write_text(
            "{totally-torn\n{also-torn\n", encoding="utf-8")
        rc, payload = _run(capsys, ["doctor", "--root", str(tmp_path),
                                    "--json", "--fix"])
        assert rc == 0
        assert payload["fixed"][0]["file"] == "healing.jsonl"
        # the fresh log carries its own quarantine event: readable again
        rows = [json.loads(line) for line in
                (tmp_path / "state" / "healing.jsonl").read_text(
                    encoding="utf-8").splitlines()]
        assert rows[0]["kind"] == KIND_DOCTOR_FIX

    def test_no_fix_flag_leaves_files_alone(self, tmp_path, capsys):
        _healthy_env(tmp_path)
        _corrupt(tmp_path, "governor_state.json")
        _rc, payload = _run(capsys, ["doctor", "--root", str(tmp_path),
                                     "--json"])
        assert "fixed" not in payload
        assert (tmp_path / "state" / "governor_state.json").is_file()

    def test_fix_over_healthy_env_is_a_noop(self, tmp_path, capsys):
        _healthy_env(tmp_path)
        HealingLog(tmp_path / "state" / "healing.jsonl").append(
            KIND_DOCTOR_FIX, "seed", ts=1.0)
        rc, payload = _run(capsys, ["doctor", "--root", str(tmp_path),
                                    "--json", "--fix"])
        assert rc == 0
        assert payload.get("fixed", []) == []
        # the healthy healing log (1 parseable row) is untouched
        assert (tmp_path / "state" / "healing.jsonl").is_file()

    def test_human_mode_renders_fixed_lines(self, tmp_path, capsys):
        _healthy_env(tmp_path)
        _corrupt(tmp_path, "token_cache.json")
        args = _parse(["doctor", "--root", str(tmp_path), "--fix"])
        rc = run_command(args.fn, args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "[FIXED]" in out
        assert "token_cache.json" in out
