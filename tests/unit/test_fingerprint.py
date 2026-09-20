"""``fbk fingerprint`` — the client-profile lifecycle family.

Unit (offline): drives the REAL ``commands.fingerprint.register`` on a
hand-assembled subparsers action — fingerprint is not yet wired into
app.build_parser, so tests pin the module's own register() seam, never
the full parser (house pattern from test_doctor.py). Every environment
is a tmp_path root reached via ``--root`` (the CLI-flag equivalent of
FBK_ROOT). The doctor-loop closure proof drives ``commands.doctor.
_check_profile`` directly over the same root's Config before/after a
freeze — doctor flips from warn-absent to pass-frozen without ever
being edited.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from commands.common import run_command
from commands.doctor import _check_profile
from commands.fingerprint import register
from config import Config
from transport.profile import ClientProfile

CHROME_136_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
CHROME_135_UA = CHROME_136_UA.replace("Chrome/136", "Chrome/135")
HEADLESS_136_UA = CHROME_136_UA.replace("Chrome/136", "HeadlessChrome/136")
# The doctor-test incoherent tuple (test_doctor.py): UA stays 136 while
# sec-ch-ua carries 135 — docs/08 §5 UA ↔ client-hint mismatch.
BROKEN_SEC_CH_UA = ('"Chromium";v="135", "Not_A Brand";v="99", '
                    '"Google Chrome";v="135"')
# A coherent non-default identity: every axis agrees on Chrome/131 —
# a REAL resolve_impersonate ladder target (chrome136 → chrome131 →
# chrome124 → chrome120, transport/session.py), never an invented one.
CHROME_131_UA = CHROME_136_UA.replace("Chrome/136", "Chrome/131")
SEC_CH_UA_131 = ('"Chromium";v="131", "Not_A Brand";v="99", '
                 '"Google Chrome";v="131"')


def _parse(argv: list[str]) -> argparse.Namespace:
    """Parse argv against fingerprint's own register() seam."""
    parser = argparse.ArgumentParser(prog="fbk", allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    register(sub)
    return parser.parse_args(argv)


def _run(capsys, argv: list[str]) -> tuple[int, str, str, dict]:
    """Drive one subcommand through the run_command barrier; return
    (rc, stdout, stderr, payload). The payload parses from the whole
    output in --json mode and from the trailing compact line in human
    mode (the emit contract)."""
    args = _parse(argv)
    rc = run_command(args.fn, args)
    captured = capsys.readouterr()
    try:
        return rc, captured.out, captured.err, json.loads(captured.out)
    except json.JSONDecodeError:
        return rc, captured.out, captured.err, json.loads(
            captured.out.splitlines()[-1])


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("FBK_ROOT", raising=False)
    monkeypatch.delenv("FBK_COOKIES", raising=False)
    monkeypatch.delenv("FBK_IMPERSONATE", raising=False)


class TestShowDefaults:
    """No frozen file: show reports the transport defaults as data — the
    "implicit defaults — not frozen" line, every field, PASS coherence."""

    def test_defaults_reported_not_frozen(self, tmp_path, capsys):
        rc, out, _, payload = _run(
            capsys, ["fingerprint", "show", "--root", str(tmp_path)])
        assert rc == 0
        assert payload["source"] == "defaults"
        assert payload["profile"]["impersonate"] == "chrome136"
        assert payload["profile"]["timezone"] == "UTC"
        assert payload["coherence_problems"] == []
        assert "implicit defaults — not frozen" in out

    def test_show_json_emits_full_model_plus_coherence(
            self, tmp_path, capsys):
        rc, out, _, payload = _run(
            capsys, ["fingerprint", "show", "--root", str(tmp_path), "--json"])
        assert rc == 0
        assert set(payload["profile"]) == set(ClientProfile().model_dump())
        assert "coherence_problems" in payload
        assert "implicit defaults" not in out  # no human decoration

    def test_incoherent_loaded_profile_is_data_not_failure(
            self, tmp_path, capsys):
        """Show never fails on coherence problems — exit 0 with the exact
        problem strings listed (module EXIT POLICY)."""
        data = tmp_path / "data"
        data.mkdir()
        ClientProfile(sec_ch_ua=BROKEN_SEC_CH_UA).save(data / "profile.json")
        rc, out, _, payload = _run(
            capsys, ["fingerprint", "show", "--root", str(tmp_path)])
        assert rc == 0
        assert payload["source"] == "frozen"
        assert payload["coherence_problems"] == [
            "UA Chrome/136 vs sec-ch-ua Chrome/135 mismatch"]
        assert "REFUSED" not in out  # show renders the verdict, never refuses

    def test_show_reports_transport_impersonate_mismatch(
            self, tmp_path, capsys):
        """Show's coherence verdict now includes the transport-level
        TLS↔UA axis: a frozen pin disagreeing with the UA major is
        reported as data with the exact transport message."""
        data = tmp_path / "data"
        data.mkdir()
        ClientProfile(impersonate="chrome124").save(data / "profile.json")
        rc, _, _, payload = _run(
            capsys, ["fingerprint", "show", "--root", str(tmp_path)])
        assert rc == 0
        assert payload["coherence_problems"] == [
            "impersonate target chrome124 disagrees with UA Chrome/136 major"]

    def test_unreadable_profile_is_data_not_failure(self, tmp_path, capsys):
        data = tmp_path / "data"
        data.mkdir()
        (data / "profile.json").write_text("}}not json", encoding="utf-8")
        rc, out, _, payload = _run(
            capsys, ["fingerprint", "show", "--root", str(tmp_path)])
        assert rc == 0
        assert payload["source"] == "unreadable"
        assert payload["profile"] is None
        assert "unreadable" in payload["error"]
        assert "UNREADABLE" in out


class TestShowFrozen:
    """A frozen file: show reports the loaded tuple with its path."""

    def test_show_reads_frozen_profile_back(self, tmp_path, capsys):
        data = tmp_path / "data"
        data.mkdir()
        ClientProfile(timezone="Europe/Berlin").save(data / "profile.json")
        rc, out, _, payload = _run(
            capsys, ["fingerprint", "show", "--root", str(tmp_path)])
        assert rc == 0
        assert payload["source"] == "frozen"
        assert payload["profile"]["timezone"] == "Europe/Berlin"
        assert "loaded from" in out
        assert str(data / "profile.json") in out


class TestFreeze:
    """Freeze persists a coherent tuple; REFUSES (exit 1, nothing
    written) on any coherence problem — docs/08 §7."""

    def test_freeze_defaults_writes_default_profile(self, tmp_path, capsys):
        rc, out, _, payload = _run(
            capsys, ["fingerprint", "freeze", "--root", str(tmp_path)])
        assert rc == 0
        assert payload["frozen"] is True
        assert payload["problems"] == []
        assert "rationale" in out
        assert "freeze a coherent profile once and replay it forever" in out
        frozen = json.loads((tmp_path / "data" / "profile.json")
                            .read_text(encoding="utf-8"))
        assert frozen == ClientProfile().model_dump()

    def test_freeze_overrides_are_persisted(self, tmp_path, capsys):
        rc, _, _, payload = _run(
            capsys, ["fingerprint", "freeze", "--root", str(tmp_path),
                     "--ua", CHROME_136_UA, "--locale", "en-GB,en;q=0.8",
                     "--tz", "Europe/Berlin", "--impersonate", "chrome136"])
        assert rc == 0
        frozen = json.loads((tmp_path / "data" / "profile.json")
                            .read_text(encoding="utf-8"))
        assert frozen["user_agent"] == CHROME_136_UA
        assert frozen["accept_language"] == "en-GB,en;q=0.8"
        assert frozen["timezone"] == "Europe/Berlin"
        assert frozen["impersonate"] == "chrome136"
        assert payload["ua_sanitized"] is False

    def test_freeze_sanitizes_headless_ua_before_persisting(
            self, tmp_path, capsys):
        """The SANITIZED value is frozen (module header): a raw
        HeadlessChrome marker must never survive into profile.json."""
        rc, _, _, payload = _run(
            capsys, ["fingerprint", "freeze", "--root", str(tmp_path),
                     "--ua", HEADLESS_136_UA])
        assert rc == 0
        assert payload["ua_sanitized"] is True
        frozen = json.loads((tmp_path / "data" / "profile.json")
                            .read_text(encoding="utf-8"))
        assert frozen["user_agent"] == CHROME_136_UA
        assert "HeadlessChrome" not in frozen["user_agent"]

    def test_freeze_then_show_then_doctor_flips_warn_to_pass(
            self, tmp_path, capsys):
        """The doctor-loop closure proof: the warn-absent profile check
        flips to pass-frozen once freeze writes the file — driven via
        doctor's own _check_profile over the same root's Config."""
        check_before, _ = _check_profile(Config.discover(tmp_path))
        assert check_before.status == "warn"
        assert "transport defaults in use" in check_before.detail
        rc, _, _, freeze = _run(
            capsys, ["fingerprint", "freeze", "--root", str(tmp_path)])
        assert rc == 0
        assert freeze["frozen"] is True
        rc_show, out, _, show = _run(
            capsys, ["fingerprint", "show", "--root", str(tmp_path)])
        assert rc_show == 0
        assert show["source"] == "frozen"
        assert "loaded from" in out
        check_after, _ = _check_profile(Config.discover(tmp_path))
        assert check_after.status == "pass"
        assert "coherent" in check_after.detail

    def test_freeze_refuses_incoherent_ua_writes_nothing(
            self, tmp_path, capsys):
        """A UA contradicting the default sec-ch-ua (docs/08 §5 major
        mismatch): exit 1, the exact problem pinned, no file written."""
        rc, out, err, payload = _run(
            capsys, ["fingerprint", "freeze", "--root", str(tmp_path),
                     "--ua", CHROME_135_UA])
        assert rc == 1
        assert payload["frozen"] is False
        # both contradicting axes are named: the UA contradicts the default
        # sec-ch-ua AND the default chrome136 TLS pin — both now via the
        # transport's validate_coherence (the command-local axis was
        # absorbed into the transport, module header).
        assert payload["problems"] == [
            "UA Chrome/135 vs sec-ch-ua Chrome/136 mismatch",
            "impersonate target chrome136 disagrees with UA Chrome/135 major"]
        assert not (tmp_path / "data" / "profile.json").exists()
        assert "REFUSED" in out
        assert "refusing to freeze" in err

    def test_freeze_refuses_impersonate_major_mismatch(
            self, tmp_path, capsys):
        """The transport-level TLS↔UA axis (validate_coherence, docs/09
        §1.2): a versioned pin disagreeing with the UA's Chrome major
        must never be frozen — regression pin for the absorbed
        command-local check."""
        rc, _, err, payload = _run(
            capsys, ["fingerprint", "freeze", "--root", str(tmp_path),
                     "--impersonate", "chrome124"])
        assert rc == 1
        assert payload["problems"] == [
            "impersonate target chrome124 disagrees with UA Chrome/136 major"]
        assert not (tmp_path / "data" / "profile.json").exists()
        assert "refusing to freeze" in err

    def test_freeze_non_default_coherent_major_via_flags(
            self, tmp_path, capsys):
        """A coherent NON-default major identity freezable end-to-end via
        the full flag set (the pre-flag escape hatch was a hand-edit):
        Chrome/131 on every axis — --ua, --sec-ch-ua, --impersonate
        chrome131 (a real ladder target) — freezes clean, exit 0."""
        rc, _, _, payload = _run(
            capsys, ["fingerprint", "freeze", "--root", str(tmp_path),
                     "--ua", CHROME_131_UA, "--sec-ch-ua", SEC_CH_UA_131,
                     "--impersonate", "chrome131"])
        assert rc == 0
        assert payload["frozen"] is True
        assert payload["problems"] == []
        frozen = json.loads((tmp_path / "data" / "profile.json")
                            .read_text(encoding="utf-8"))
        assert frozen["user_agent"] == CHROME_131_UA
        assert frozen["sec_ch_ua"] == SEC_CH_UA_131
        assert frozen["impersonate"] == "chrome131"

    def test_freeze_client_hint_flags_are_persisted(self, tmp_path, capsys):
        """The platform/mobile client-hint flags override the model
        defaults and persist; unset hints still inherit the defaults."""
        rc, _, _, payload = _run(
            capsys, ["fingerprint", "freeze", "--root", str(tmp_path),
                     "--sec-ch-ua-platform", '"Windows"',
                     "--sec-ch-ua-mobile", "?0"])
        assert rc == 0
        assert payload["problems"] == []
        frozen = json.loads((tmp_path / "data" / "profile.json")
                            .read_text(encoding="utf-8"))
        assert frozen["sec_ch_ua_platform"] == '"Windows"'
        assert frozen["sec_ch_ua_mobile"] == "?0"


class TestClear:
    """Clear is idempotent: removing a present file and clearing an
    already-clear root are both exit 0."""

    def test_clear_present_removes_file(self, tmp_path, capsys):
        _, _, _, _ = _run(
            capsys, ["fingerprint", "freeze", "--root", str(tmp_path)])
        rc, out, _, payload = _run(
            capsys, ["fingerprint", "clear", "--root", str(tmp_path)])
        assert rc == 0
        assert payload["removed"] is True
        assert "removed frozen profile" in out
        assert not (tmp_path / "data" / "profile.json").exists()
        # show flips back to the implicit defaults
        _, _, _, show = _run(
            capsys, ["fingerprint", "show", "--root", str(tmp_path)])
        assert show["source"] == "defaults"

    def test_clear_absent_is_success(self, tmp_path, capsys):
        rc, out, _, payload = _run(
            capsys, ["fingerprint", "clear", "--root", str(tmp_path)])
        assert rc == 0
        assert payload["removed"] is False
        assert "no frozen profile" in out


class TestFingerprintWiring:
    """fingerprint parses on its own register() seam (the orchestrator
    wires it into app.py; these tests never use build_parser)."""

    @pytest.mark.parametrize("argv,child", (
        (["fingerprint", "show"], "show"),
        (["fingerprint", "freeze"], "freeze"),
        (["fingerprint", "clear"], "clear"),
    ))
    def test_children_parse_with_defaults(self, argv, child):
        args = _parse(argv)
        assert args.fingerprint_command == child
        assert callable(args.fn)

    def test_freeze_flags_parse(self):
        args = _parse(["fingerprint", "freeze", "--ua", CHROME_136_UA,
                       "--locale", "en-US,en;q=0.9", "--tz", "UTC",
                       "--impersonate", "chrome136",
                       "--sec-ch-ua", SEC_CH_UA_131.replace("131", "136"),
                       "--sec-ch-ua-platform", '"Windows"',
                       "--sec-ch-ua-mobile", "?0"])
        assert args.ua == CHROME_136_UA
        assert args.sec_ch_ua == ('"Chromium";v="136", "Not_A Brand";v="99", '
                                  '"Google Chrome";v="136"')
        assert args.sec_ch_ua_platform == '"Windows"'
        assert args.sec_ch_ua_mobile == "?0"
        assert args.timezone == "UTC"

    def test_common_flags_on_children(self):
        args = _parse(["fingerprint", "show", "--root", "X", "--json"])
        assert args.as_json is True
        assert callable(args.fn)

    def test_family_requires_a_child(self, capsys):
        with pytest.raises(SystemExit) as ei:
            _parse(["fingerprint"])
        assert ei.value.code == 2
        assert "required" in capsys.readouterr().err
