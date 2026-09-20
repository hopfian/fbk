"""``fbk registry audit`` — KNOWN_MUTATIONS vs the loaded registry.

Unit (offline): runs against the REAL data/ registries (the actual
shipped files) and pins today's audit verdict, then drives synthetic
tmp_path registries via ``--root`` isolation to exercise the
doc-id-mismatch and missing statuses, and the typed exit-6 path when
the registry itself cannot load.

HONEST BASELINE: the shipped v3 registry carries 16 of the 17 catalog
entries with matching doc_ids — ``CometUFIDeleteCommentMutation`` is
absent from EVERY shipped registry file (v2 and v3 both lack it; they
carry ``useCometUFIDeleteCommentMutation`` instead, a different
friendly name). That is exactly the silent gap this subcommand exists
to surface: live mutation traffic is unaffected (surfaces resolve
KNOWN_MUTATIONS first, so the catalog id is sent), but the registry
fallback for that name would raise RegistryMissError.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import build_parser
from commands.common import run_command
from constants import KNOWN_MUTATIONS

CLI_ROOT = Path(__file__).resolve().parents[2]

# The real shipped registries' audit verdict, pinned like the pair
# counts in test_registry_from_file.py (the data/ files are shipped).
_V3_NAME = "doc_id_registry_v3.json"
#: Registry-orphans in the catalog: the comment-delete registration (absent
#: from every harvest) plus the three bundle-decoded story operations the
#: lazy composer chunks carry (create/reply/viewers - resolved via their
#: baked constants, never via the registry).
_MISSING_NAMES = [
    "CometUFIDeleteCommentMutation",
    "StoriesCreateMutation",
    "useStoriesSendReplyMutation",
    "StoriesSuspenseViewerSheetViewerListV2Query",
]
_MISMATCH_NAME = "useCometUFICreateCommentMutation"
_OK_COUNT = len(KNOWN_MUTATIONS) - len(_MISSING_NAMES)


def _clear_env(monkeypatch) -> None:
    monkeypatch.delenv("FBK_ROOT", raising=False)
    monkeypatch.delenv("FBK_COOKIES", raising=False)
    monkeypatch.delenv("FBK_IMPERSONATE", raising=False)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    _clear_env(monkeypatch)


def _run_json(capsys, argv) -> tuple[int, dict]:
    """Run one audit in --json mode; return (rc, payload)."""
    args = build_parser().parse_args(argv)
    rc = args.fn(args)
    return rc, json.loads(capsys.readouterr().out)


def _write_registry(data_dir: Path, name: str, pairs: dict[str, str],
                   revision: str = "synthetic") -> None:
    """Write one synthetic registry file (the unique_pairs schema)."""
    doc = {"revision": revision,
           "unique_pairs": [{"friendly_name": n, "doc_id": d}
                            for n, d in sorted(pairs.items())]}
    (data_dir / name).write_text(json.dumps(doc), encoding="utf-8")


class TestAuditRealData:
    """Pins the audit on the REAL shipped data/: ok all-but-orphans,
    4 registry-orphans (the never-harvested comment-delete registration
    plus the three bundle-decoded story operations), zero doc-id
    mismatches. (The comment-create entry WAS a mismatch right after the
    revision-1047963790 harvest rotated its registration; the catalog was
    re-pinned to the fresh id 39607465588840384 after the live
    field_exception 1357010 confirmed the old registration died with the
    deploy - the audit tracked both states.)"""

    def test_real_data_verdict_is_pinned(self, capsys):
        rc, payload = _run_json(
            capsys, ["registry", "audit", "--json", "--root", str(CLI_ROOT)])
        assert rc == 0
        assert payload["registry"] == _V3_NAME
        assert payload["catalog_entries"] == len(KNOWN_MUTATIONS)
        assert payload["counts"] == {"ok": _OK_COUNT, "doc-id-mismatch": 0,
                                      "missing": len(_MISSING_NAMES)}
        missing = [e["name"] for e in payload["entries"]
                   if e["status"] == "missing"]
        assert missing == _MISSING_NAMES
        assert [e["name"] for e in payload["entries"]
                if e["status"] == "doc-id-mismatch"] == []
        # every other entry matches the registry doc_id exactly
        for e in payload["entries"]:
            if e["status"] == "ok":
                assert e["catalog_doc_id"] == e["registry_doc_id"]

    def test_human_output_renders_status_lines_and_summary(self, capsys):
        args = build_parser().parse_args(["registry", "audit",
                                          "--root", str(CLI_ROOT)])
        rc = args.fn(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert f"catalog: {len(KNOWN_MUTATIONS)} KNOWN_MUTATIONS entries vs " \
               f"registry {_V3_NAME}" in out
        assert "ok " in out  # per-entry status lines render
        for name in _MISSING_NAMES:
            assert f"missing         {name}" in out
        assert f"summary: ok {_OK_COUNT}, doc-id-mismatch 0, " \
               f"missing {len(_MISSING_NAMES)}" in out
        assert "query catalog: none" in out
        # problems present → the docs/13 §2 recovery pointer renders
        assert "recovery: re-harvest with `fbk registry refresh --save`" in out
        # emit contract: the trailing compact JSON line closes the output
        json.loads(out.splitlines()[-1])


class TestAuditSynthetic:
    """Drives the mismatch/missing statuses on synthetic tmp_path
    registries (--root isolation): a catalog name absent from the
    registry and one whose registry doc_id drifted (the REPLAY RISK —
    we would send the wrong persisted-doc hash)."""

    def test_one_missing_and_one_mismatched_entry(self, tmp_path, capsys):
        pairs = dict(KNOWN_MUTATIONS)
        pairs["CometPageFollowCommitMutation"] = "1"  # drifted: replay risk
        del pairs["CometPageLikeCommitMutation"]  # absent: future miss
        data = tmp_path / "data"
        data.mkdir()
        _write_registry(data, _V3_NAME, pairs)
        rc, payload = _run_json(
            capsys, ["registry", "audit", "--json", "--root", str(tmp_path)])
        assert rc == 0  # mismatches and misses are DATA, never failure
        assert payload["counts"] == {
            "ok": len(KNOWN_MUTATIONS) - 2, "doc-id-mismatch": 1, "missing": 1}
        by_name = {e["name"]: e for e in payload["entries"]}
        mismatch = by_name["CometPageFollowCommitMutation"]
        assert mismatch["status"] == "doc-id-mismatch"
        assert mismatch["catalog_doc_id"] \
            == KNOWN_MUTATIONS["CometPageFollowCommitMutation"]
        assert mismatch["registry_doc_id"] == "1"
        assert by_name["CometPageLikeCommitMutation"]["status"] == "missing"
        assert by_name["CometPageLikeCommitMutation"]["registry_doc_id"] is None

    def test_all_ok_registry_has_no_recovery_line(self, tmp_path, capsys):
        data = tmp_path / "data"
        data.mkdir()
        _write_registry(data, _V3_NAME, dict(KNOWN_MUTATIONS))
        args = build_parser().parse_args(["registry", "audit",
                                          "--root", str(tmp_path)])
        rc = args.fn(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert f"summary: ok {len(KNOWN_MUTATIONS)}, doc-id-mismatch 0, " \
               f"missing 0" in out
        assert "recovery:" not in out


class TestAuditTypedFailure:
    """A registry that cannot LOAD is the audit's only error path —
    the existing typed RegistryMissError family, exit 6 (never a raw
    JSONDecodeError or KeyError)."""

    def test_missing_registry_is_typed_exit_6(self, tmp_path, capsys):
        data = tmp_path / "data"
        data.mkdir()  # no registry file at all
        args = build_parser().parse_args(["registry", "audit",
                                          "--root", str(tmp_path)])
        rc = run_command(args.fn, args)
        err = capsys.readouterr().err
        assert rc == 6
        assert "no registry file" in err

    def test_corrupt_registry_is_typed_exit_6(self, tmp_path, capsys):
        data = tmp_path / "data"
        data.mkdir()
        (data / _V3_NAME).write_text("{oops not json", encoding="utf-8")
        args = build_parser().parse_args(["registry", "audit",
                                          "--root", str(tmp_path)])
        rc = run_command(args.fn, args)
        err = capsys.readouterr().err
        assert rc == 6
        assert "corrupt" in err


class TestAuditWiring:
    """Pins the subcommand on the real app parser."""

    def test_audit_parses_with_handler(self):
        args = build_parser().parse_args(["registry", "audit"])
        assert args.command == "registry"
        assert args.registry_command == "audit"
        assert callable(args.fn)

    def test_registry_family_requires_a_child(self, capsys):
        with pytest.raises(SystemExit) as ei:
            build_parser().parse_args(["registry"])
        assert ei.value.code == 2
        assert "required" in capsys.readouterr().err
