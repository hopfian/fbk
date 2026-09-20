"""``fbk templates`` — the captured-mutation census (docs/15 §P2-3/P3).

Unit (offline): drives the family's OWN register()-built parser (the
module is not wired into app.py yet — no build_parser dependency) via
direct construction, against BOTH the REAL cli/data assets (they are
synthetic-scrubbed, safe: list counts and registry cross-refs are
pinned to the shapes read off those files) and synthetic tmp_path
roots via ``--root`` (broken assets, orphan doc_ids, and the hard
secret-redaction pin).

SECURITY PIN: the synthetic show test plants a KNOWN secret value
under every secret-name family (cookie ``xs``, token ``fb_dtsg``,
surface token ``privacy_write_id``) and asserts the value NEVER
reaches stdout in ANY show output mode (human and --json) — only the
``<redacted:>`` markers may appear.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from commands import templates

CLI_ROOT = Path(__file__).resolve().parents[2]
DATA = CLI_ROOT / "data"

# The planted never-print value (the hard redaction pin).
_SECRET = "PINNED-SECRET-7c31f9a4-never-print-me"
_PLAIN = "plain-session-value-must-print"


def _clear_env(monkeypatch) -> None:
    monkeypatch.delenv("FBK_ROOT", raising=False)
    monkeypatch.delenv("FBK_COOKIES", raising=False)


def _parser() -> argparse.ArgumentParser:
    """The templates family wired the way app.py will wire it (direct
    register() — the module is not in _COMMAND_MODULES yet)."""
    parser = argparse.ArgumentParser(prog="fbk", allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    templates.register(sub)
    return parser


def _run(capsys, argv) -> tuple[int, str, str]:
    """Parse+run one templates invocation; return (rc, stdout, stderr)."""
    args = _parser().parse_args(argv)
    rc = args.fn(args)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _payload(out: str) -> dict[str, Any]:
    """The command payload: the whole output in --json mode, the trailing
    compact line in human mode (the emit contract)."""
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return json.loads(out.splitlines()[-1])


def _asset(payload: dict, name: str) -> dict:
    """One asset block from a list/verify payload, by file name."""
    return next(a for a in payload["assets"] if a["asset"] == name)


def _write_json(path: Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def _write_registry(root: Path, pairs: list[tuple[str, str]]) -> None:
    """A synthetic v3 registry (the freshest from_assets candidate)."""
    _write_json(root / "data" / "doc_id_registry_v3.json",
                {"unique_pairs": [{"friendly_name": f, "doc_id": d,
                                   "sources": ["carried"]}
                                  for f, d in pairs]})


class TestTemplatesList:
    """Census over the REAL data assets: counts and registry cross-refs
    pinned to the shapes read off the shipped files."""

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        _clear_env(monkeypatch)

    def test_mutation_counts_match_the_assets(self, capsys):
        rc, out, _ = _run(capsys, ["templates", "list", "--json"])
        assert rc == 0
        assert _asset(_payload(out), "captured_mutations.json")["count"] == 2
        assert _asset(_payload(out), "captured_composer.json")["count"] == 3
        assert _asset(_payload(out),
                      "captured_comment_mutations.json")["count"] == 6

    def test_registry_cross_refs_resolve(self, capsys):
        """Captured doc_ids cross-reference the v3 registry. After the
        full-harvest refresh (revision 1047963790) the comment-create
        registration ROTATED: the capture pins the live-verified
        28882491998025365, the registry carries the current
        39607465588840384 - the census must report that entry honestly
        (in_registry False + the registry id alongside); every other
        capture matches exactly."""
        rc, out, _ = _run(capsys, ["templates", "list", "--json"])
        assert rc == 0
        composer = _asset(_payload(out), "captured_composer.json")
        names = {m["friendly_name"] for m in composer["mutations"]}
        assert names == {
            "useHasSeenUnifiedVideoCreationPrivacyDisclaimerMutation",
            "CometPrivacySelectorSavePrivacyMutation",
            "ComposerStoryCreateMutation",
        }
        rotated = 0
        for asset in _payload(out)["assets"]:
            for m in asset.get("mutations", []):
                if m["friendly_name"] == "useCometUFICreateCommentMutation":
                    assert m["in_registry"] is False
                    assert m["registry_doc_id"] == "39607465588840384"
                    assert m["doc_id"] == "28882491998025365"
                    rotated += 1
                else:
                    assert m["in_registry"] is True
                    assert m["registry_doc_id"] == m["doc_id"]
        assert rotated == 1

    def test_delta_capture_reported_by_its_actual_shape(self, capsys):
        """captured_real_deltas.json is NOT a mutations file (thread_id +
        sent + mqtt/dgw frames) — the census must say what it actually
        holds, not force the mutations frame."""
        rc, out, _ = _run(capsys, ["templates", "list", "--json"])
        assert rc == 0
        deltas = _asset(_payload(out), "captured_real_deltas.json")
        assert deltas["kind"] == "delta capture"
        assert deltas["shape"]["dgw_frames"] == "list[17]"
        assert deltas["shape"]["mqtt_frames"] == "list[0]"
        assert "mutations" not in deltas

    def test_human_mode_names_every_mutation(self, capsys):
        rc, out, _ = _run(capsys, ["templates", "list"])
        assert rc == 0
        for fragment in ("captured_mutations.json",
                         "CometUFIFeedbackReactMutation",
                         "27646120298312844",
                         "in registry",
                         "captured_real_deltas.json — delta capture"):
            assert fragment in out


class TestTemplatesShow:
    """The census drill-down: full captured variables, secrets redacted,
    tracking ciphertext truncated."""

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        _clear_env(monkeypatch)

    def test_real_privacy_mutation_secret_never_prints(self, capsys):
        """The composer's privacy capture carries a secret-named
        privacy_write_id (docs/15 §P3) — its raw value must never reach
        stdout; only the journal redaction marker may appear."""
        rc, out, _ = _run(
            capsys, ["templates", "show", "--json",
                     "--asset", "captured_composer.json",
                     "--mutation", "CometPrivacySelectorSavePrivacyMutation"])
        assert rc == 0
        raw = json.loads((DATA / "captured_composer.json").read_text(
            encoding="utf-8"))
        privacy = raw["mutations"][1]["variables"]["input"]["privacy_write_id"]
        assert privacy not in out
        assert "<redacted:" in out
        payload = _payload(out)
        assert payload["doc_id"] == "27802157519437974"
        redacted = payload["variables"]["input"]["privacy_write_id"]
        assert redacted.startswith("<redacted:")

    def test_real_tracking_ciphertext_is_truncated(self, capsys):
        """The react capture's tracking blob (1391 chars of verbatim-replay
        ciphertext, docs/15 §P2-3) prints head-only with a length note."""
        rc, out, _ = _run(
            capsys, ["templates", "show", "--json",
                     "--asset", "captured_mutations.json",
                     "--mutation", "CometUFIFeedbackReactMutation"])
        assert rc == 0
        raw = json.loads((DATA / "captured_mutations.json").read_text(
            encoding="utf-8"))
        blob = raw["mutations"][0]["variables"]["input"]["tracking"][0]
        assert blob not in out            # the full ciphertext never dumps
        assert blob[:200] in out          # the head identifies the capture
        assert f"[+{len(blob) - 200} chars truncated]" in out

    def test_synthetic_secret_pinned_in_every_mode(self, tmp_path, capsys):
        """THE REDACTION PIN: a planted secret under one name per family
        (cookie xs, token fb_dtsg, surface token privacy_write_id) never
        reaches stdout in ANY show output mode; non-secret values print."""
        doc = {"mutations": [{
            "friendly_name": "PinMutation",
            "doc_id": "111",
            "variables": {"input": {
                "xs": _SECRET,
                "fb_dtsg": _SECRET,
                "privacy_write_id": _SECRET,
                "session_id": _PLAIN,
                "tracking": ["B" * 500],
            }},
        }]}
        _write_json(tmp_path / "data" / "captured_pin.json", doc)
        for argv in (["templates", "show", "--asset", "captured_pin.json",
                      "--mutation", "PinMutation"],
                     ["templates", "show", "--asset", "captured_pin.json",
                      "--mutation", "PinMutation", "--json"]):
            rc, out, _ = _run(capsys, [*argv, "--root", str(tmp_path)])
            assert rc == 0
            assert _SECRET not in out     # the security boundary
            assert out.count("<redacted:") >= 3
            assert _PLAIN in out          # non-secret values pass through

    def test_first_friendly_match_wins(self, tmp_path, capsys):
        """Same-named captures (like-vs-remove reactions) resolve to the
        FIRST entry — load_template semantics."""
        doc = {"mutations": [
            {"friendly_name": "DupMutation", "doc_id": "1",
             "variables": {"input": {"client_mutation_id": "first"}}},
            {"friendly_name": "DupMutation", "doc_id": "2",
             "variables": {"input": {"client_mutation_id": "second"}}},
        ]}
        _write_json(tmp_path / "data" / "captured_dup.json", doc)
        rc, out, _ = _run(capsys, ["templates", "show", "--json",
                                   "--asset", "captured_dup.json",
                                   "--mutation", "DupMutation",
                                   "--root", str(tmp_path)])
        assert rc == 0
        payload = _payload(out)
        assert payload["doc_id"] == "1"
        assert payload["variables"]["input"]["client_mutation_id"] == "first"

    def test_unknown_mutation_exits_1_and_names_candidates(self, capsys):
        rc, _, err = _run(capsys, ["templates", "show",
                                   "--asset", "captured_composer.json",
                                   "--mutation", "NoSuchMutationEver"])
        assert rc == 1
        assert "NoSuchMutationEver" in err
        assert "ComposerStoryCreateMutation" in err  # the real candidates

    def test_missing_asset_exits_1(self, capsys):
        rc, _, err = _run(capsys, ["templates", "show",
                                   "--asset", "captured_absent.json",
                                   "--mutation", "X"])
        assert rc == 1
        assert "captured_absent.json" in err

    def test_non_mutations_asset_rejected(self, capsys):
        """Showing the delta capture under the mutations frame is a failed
        precondition, not a crash."""
        rc, _, err = _run(capsys, ["templates", "show",
                                   "--asset", "captured_real_deltas.json",
                                   "--mutation", "X"])
        assert rc == 1
        assert "no 'mutations' list" in err


class TestTemplatesVerify:
    """The structural gate: orphans and entry-shape gaps WARN (data),
    parse failures CRITICAL (exit 1)."""

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        _clear_env(monkeypatch)

    def test_real_data_verifies_clean(self, capsys):
        """All 11 captured mutations parse; zero critical. ONE warning is
        expected post-refresh (revision 1047963790): the comment-create
        capture's doc_id (28882491998025365, live-verified) rotated in the
        current registry (39607465588840384) - an id-rotation warning, not
        corruption."""
        rc, out, _ = _run(capsys, ["templates", "verify", "--json"])
        assert rc == 0
        payload = _payload(out)
        assert payload["status"] == "ok"
        assert payload["critical"] == 0
        assert payload["warnings"] == 1
        assert _asset(payload, "captured_real_deltas.json")["status"] == "ok"

    def test_human_mode_renders_the_verdict(self, capsys):
        rc, out, _ = _run(capsys, ["templates", "verify"])
        assert rc == 0
        assert "verify PASS" in out
        assert "CRITICAL" not in out
        assert "captured_real_deltas.json" in out  # ok line, no mutation checks

    def test_broken_asset_is_critical_exit_1(self, tmp_path, capsys):
        data = tmp_path / "data"
        data.mkdir(parents=True)
        (data / "captured_broken.json").write_text("{ this is not json",
                                                   encoding="utf-8")
        _write_registry(tmp_path, [("HealthyMutation", "111")])
        rc, out, _ = _run(capsys, ["templates", "verify", "--json",
                                   "--root", str(tmp_path)])
        assert rc == 1
        payload = _payload(out)
        assert payload["status"] == "critical"
        assert payload["critical"] == 1

    def test_orphans_and_shape_gaps_warn_but_exit_0(self, tmp_path, capsys):
        """An orphan doc_id and an empty-variables entry are registry
        refresh / re-capture candidates — WARN lines, never gate failures."""
        doc = {"mutations": [
            {"friendly_name": "OrphanMutation", "doc_id": "999",
             "variables": {"input": {"a": 1}}},
            {"friendly_name": "HollowMutation", "doc_id": "111",
             "variables": {}},
            {"friendly_name": "HealthyMutation", "doc_id": "111",
             "variables": {"input": {"a": 1}}},
        ]}
        _write_json(tmp_path / "data" / "captured_mixed.json", doc)
        _write_registry(tmp_path, [("HealthyMutation", "111")])
        rc, out, _ = _run(capsys, ["templates", "verify", "--json",
                                   "--root", str(tmp_path)])
        assert rc == 0
        payload = _payload(out)
        assert payload["status"] == "ok"
        assert payload["critical"] == 0
        assert payload["warnings"] == 2
        issues = _asset(payload, "captured_mixed.json")["issues"]
        assert any("orphan" in i for i in issues)
        assert any("empty variables" in i for i in issues)

    def test_broken_mutations_schema_is_critical_exit_1(self, tmp_path, capsys):
        """'mutations' present but not a list = the asset cannot be
        interpreted at all — CRITICAL, same as a parse failure."""
        _write_json(tmp_path / "data" / "captured_schema.json",
                    {"mutations": {"friendly_name": "oops"}})
        _write_registry(tmp_path, [("X", "1")])
        rc, out, _ = _run(capsys, ["templates", "verify", "--json",
                                   "--root", str(tmp_path)])
        assert rc == 1
        assert _payload(out)["critical"] == 1


class TestTemplatesRegisterWiring:
    """register() construction (app.py wiring is the orchestrator's): the
    family parses with its children and requires one, house convention."""

    @pytest.mark.parametrize("argv", (
        ["templates", "list"],
        ["templates", "show", "--asset", "a.json", "--mutation", "m"],
        ["templates", "verify"],
    ))
    def test_children_parse_with_handlers(self, argv):
        args = _parser().parse_args(argv)
        assert callable(args.fn)

    def test_show_flags_bind_to_the_namespace(self):
        args = _parser().parse_args(
            ["templates", "show", "--asset", "captured_mutations.json",
             "--mutation", "CometUFIFeedbackReactMutation"])
        assert args.asset == "captured_mutations.json"
        assert args.mutation == "CometUFIFeedbackReactMutation"

    def test_family_requires_a_child(self, capsys):
        with pytest.raises(SystemExit) as ei:
            _parser().parse_args(["templates"])
        assert ei.value.code == 2
        assert "required" in capsys.readouterr().err
