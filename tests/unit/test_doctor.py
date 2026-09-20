"""``fbk doctor`` — the offline environment self-diagnostic.

Unit (offline): a tmp_path-rooted environment built through Config
(the ``--root`` CLI-flag equivalent of FBK_ROOT, house pattern from
test_config_show.py) drives the REAL ``commands.doctor.register`` on a
hand-assembled subparsers action — doctor is not yet wired into
app.build_parser, so tests pin the module's own register() seam, never
the full parser.

The impersonate probe is stubbed for speed in most tests (the real
``resolve_impersonate`` hits 127.0.0.1:9, docs/12 §3 — local and
offline-safe, but ~200ms of curl_cffi import); one test runs the real
probe to pin the offline claim.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import commands.doctor as doctor_mod
from app import _VERSION
from commands.common import run_command
from commands.doctor import _probe_state_writable, register
from transport.profile import ClientProfile
from transport.session import FingerprintRejectedError

CHECK_ORDER = ["registry", "capture assets", "client profile", "impersonate",
               "cookie jar", "state dir", "journal dir", "version"]

JAR_AUTHED = (
    "# Netscape HTTP Cookie File\n"
    ".facebook.com\tTRUE\t/\tTRUE\t1999999999\tc_user\t1234567890\n"
    ".facebook.com\tTRUE\t/\tTRUE\t1999999999\txs\t7:AbCdEfSecret\n"
)
JAR_NO_AUTH = (
    "# Netscape HTTP Cookie File\n"
    ".facebook.com\tTRUE\t/\tTRUE\t1999999999\tdatr\tDeviceTokenValue1\n"
)


def _parse(argv: list[str]) -> argparse.Namespace:
    """Parse argv against doctor's own register() seam (not build_parser)."""
    parser = argparse.ArgumentParser(prog="fbk", allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    register(sub)
    return parser.parse_args(argv)


def _run(capsys, argv: list[str]) -> tuple[int, str, dict]:
    """Drive ``doctor`` through the run_command barrier; return (rc, stdout,
    payload). The payload parses from the whole output in --json mode and
    from the trailing compact line in human mode (the emit contract)."""
    args = _parse(argv)
    rc = run_command(args.fn, args)
    out = capsys.readouterr().out
    try:
        return rc, out, json.loads(out)
    except json.JSONDecodeError:
        return rc, out, json.loads(out.splitlines()[-1])


def _healthy_env(root: Path, *, cookies: str | None = None) -> None:
    """Build a fully healthy offline env: both registry tiers, every
    referenced capture asset, a writable state/ dir, optional jar."""
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
    if cookies is not None:
        (root / "cookies.txt").write_text(cookies, encoding="utf-8")


def _stub_probe(monkeypatch, result: str = "chrome136") -> None:
    """Stub the impersonate resolution (local probe, but slow to import)."""
    monkeypatch.setattr(doctor_mod, "resolve_impersonate",
                        lambda preferred: result)


def _check(payload: dict, name: str) -> dict:
    """One check's dict by name — the ordered list is pinned separately."""
    return next(c for c in payload["checks"] if c["name"] == name)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("FBK_ROOT", raising=False)
    monkeypatch.delenv("FBK_COOKIES", raising=False)
    monkeypatch.delenv("FBK_IMPERSONATE", raising=False)


class TestDoctorHealthy:
    """Full-healthy offline env: exit 0, pinned JSON shape; the absent jar
    and the absent profile are WARNs, never failures."""

    def test_healthy_env_exits_zero_with_pinned_order(
            self, tmp_path, monkeypatch, capsys):
        _healthy_env(tmp_path)
        _stub_probe(monkeypatch)
        rc, _, payload = _run(capsys, ["doctor", "--root", str(tmp_path),
                                       "--json"])
        assert rc == 0
        assert payload["ok"] is True
        assert [c["name"] for c in payload["checks"]] == CHECK_ORDER

    def test_json_shape_is_pinned(self, tmp_path, monkeypatch, capsys):
        _healthy_env(tmp_path)
        _stub_probe(monkeypatch)
        _, _, payload = _run(capsys, ["doctor", "--root", str(tmp_path),
                                      "--json"])
        assert set(payload) == {"ok", "checks"}
        for c in payload["checks"]:
            assert set(c) == {"name", "status", "critical", "detail", "hint"}
        critical = {c["name"]: c["critical"] for c in payload["checks"]}
        assert critical == {
            "registry": True, "capture assets": True,
            "client profile": True, "impersonate": True,
            "cookie jar": False, "state dir": True,
            "journal dir": False, "version": False,
        }

    def test_cookies_absent_warns_but_exits_zero(
            self, tmp_path, monkeypatch, capsys):
        """The post-scrub headline: doctor must PASS on a clean offline env
        with no jar, carrying the exit-7 warning line."""
        _healthy_env(tmp_path)
        _stub_probe(monkeypatch)
        rc, out, payload = _run(capsys, ["doctor", "--root", str(tmp_path)])
        assert rc == 0
        jar = _check(payload, "cookie jar")
        assert jar["status"] == "warn"
        assert "exit 7" in jar["detail"]
        assert jar["hint"] is not None
        assert "[WARN] cookie jar" in out

    def test_cookies_auth_pair_complete_passes(
            self, tmp_path, monkeypatch, capsys):
        _healthy_env(tmp_path, cookies=JAR_AUTHED)
        _stub_probe(monkeypatch)
        rc, _, payload = _run(capsys, ["doctor", "--root", str(tmp_path)])
        assert rc == 0
        jar = _check(payload, "cookie jar")
        assert jar["status"] == "pass"
        assert "c_user + xs" in jar["detail"]

    def test_cookies_missing_auth_pair_warns(
            self, tmp_path, monkeypatch, capsys):
        _healthy_env(tmp_path, cookies=JAR_NO_AUTH)
        _stub_probe(monkeypatch)
        rc, _, payload = _run(capsys, ["doctor", "--root", str(tmp_path)])
        assert rc == 0
        jar = _check(payload, "cookie jar")
        assert jar["status"] == "warn"
        assert "c_user" in jar["detail"]

    def test_human_mode_renders_lines_then_trailing_json(
            self, tmp_path, monkeypatch, capsys):
        _healthy_env(tmp_path)
        _stub_probe(monkeypatch)
        rc, out, _ = _run(capsys, ["doctor", "--root", str(tmp_path)])
        assert rc == 0
        assert out.count("[PASS]") >= 5
        assert "[WARN]" in out
        assert "doctor: 8 checks" in out
        assert "environment healthy" in out
        # the trailing compact line is the machine payload (emit contract)
        assert json.loads(out.splitlines()[-1])["ok"] is True

    def test_state_dir_writable_governor_state_parses(
            self, tmp_path, monkeypatch, capsys):
        _healthy_env(tmp_path)
        (tmp_path / "state" / "governor_state.json").write_text(
            json.dumps({"day": "2026-09-19", "day_count": 3,
                        "cooldown_until": 0.0}), encoding="utf-8")
        _stub_probe(monkeypatch)
        rc, _, payload = _run(capsys, ["doctor", "--root", str(tmp_path)])
        assert rc == 0
        state = _check(payload, "state dir")
        assert state["status"] == "pass"
        assert "day_count=3" in state["detail"]

    def test_journal_count_is_informational(self, tmp_path, monkeypatch, capsys):
        _healthy_env(tmp_path)
        (tmp_path / "state" / "session.jsonl").write_text(
            '{"ts": 1}\n', encoding="utf-8")
        _stub_probe(monkeypatch)
        rc, _, payload = _run(capsys, ["doctor", "--root", str(tmp_path)])
        assert rc == 0
        journals = _check(payload, "journal dir")
        assert journals["status"] == "pass"
        assert journals["critical"] is False
        assert "1 journal file(s)" in journals["detail"]


class TestDoctorRegistry:
    """Registry tiers: v3 corrupt fails the run naming the file; v2-only
    degradation is a warn (fail-soft, reads still serve from v3)."""

    def test_v3_corrupt_fails_exit_1_naming_the_file(
            self, tmp_path, monkeypatch, capsys):
        _healthy_env(tmp_path)
        _stub_probe(monkeypatch)
        (tmp_path / "data" / "doc_id_registry_v3.json").write_text(
            "{ not json", encoding="utf-8")
        rc, _, payload = _run(capsys, ["doctor", "--root", str(tmp_path),
                                       "--json"])
        assert rc == 1
        assert payload["ok"] is False
        reg = _check(payload, "registry")
        assert reg["status"] == "fail"
        assert reg["critical"] is True
        assert "doc_id_registry_v3.json" in reg["detail"]
        assert reg["hint"] is not None

    def test_v3_missing_fails(self, tmp_path, monkeypatch, capsys):
        _healthy_env(tmp_path)
        _stub_probe(monkeypatch)
        (tmp_path / "data" / "doc_id_registry_v3.json").unlink()
        rc, _, payload = _run(capsys, ["doctor", "--root", str(tmp_path)])
        assert rc == 1
        assert _check(payload, "registry")["status"] == "fail"

    def test_v2_corrupt_with_v3_healthy_warns(
            self, tmp_path, monkeypatch, capsys):
        _healthy_env(tmp_path)
        _stub_probe(monkeypatch)
        (tmp_path / "data" / "doc_id_registry_v2.json").write_text(
            "[", encoding="utf-8")
        rc, _, payload = _run(capsys, ["doctor", "--root", str(tmp_path)])
        assert rc == 0
        reg = _check(payload, "registry")
        assert reg["status"] == "warn"
        assert "doc_id_registry_v2.json" in reg["detail"]

    def test_healthy_registry_reports_pairs_and_revision(
            self, tmp_path, monkeypatch, capsys):
        _healthy_env(tmp_path)
        _stub_probe(monkeypatch)
        _, _, payload = _run(capsys, ["doctor", "--root", str(tmp_path)])
        reg = _check(payload, "registry")
        assert reg["status"] == "pass"
        assert "2 pairs" in reg["detail"]
        assert "rev-test" in reg["detail"]


class TestDoctorCaptures:
    """Every code-referenced capture asset must parse with a non-empty
    mutations list — a missing or empty asset is CRITICAL."""

    def test_missing_capture_asset_fails(self, tmp_path, monkeypatch, capsys):
        _healthy_env(tmp_path)
        _stub_probe(monkeypatch)
        (tmp_path / "data" / "captured_mutations.json").unlink()
        rc, _, payload = _run(capsys, ["doctor", "--root", str(tmp_path)])
        assert rc == 1
        cap = _check(payload, "capture assets")
        assert cap["status"] == "fail"
        assert "captured_mutations.json" in cap["detail"]

    def test_empty_mutations_list_fails(self, tmp_path, monkeypatch, capsys):
        _healthy_env(tmp_path)
        _stub_probe(monkeypatch)
        (tmp_path / "data" / "captured_composer.json").write_text(
            json.dumps({"mutations": []}), encoding="utf-8")
        rc, _, payload = _run(capsys, ["doctor", "--root", str(tmp_path)])
        assert rc == 1
        cap = _check(payload, "capture assets")
        assert cap["status"] == "fail"
        assert "empty mutations list" in cap["detail"]

    def test_corrupt_capture_asset_fails(self, tmp_path, monkeypatch, capsys):
        _healthy_env(tmp_path)
        _stub_probe(monkeypatch)
        (tmp_path / "data" / "captured_comment_mutations.json").write_text(
            "{oops", encoding="utf-8")
        rc, _, payload = _run(capsys, ["doctor", "--root", str(tmp_path)])
        assert rc == 1
        assert _check(payload, "capture assets")["status"] == "fail"


class TestDoctorProfile:
    """Absent profile is an advisory WARN (transport defaults in use);
    a present-but-incoherent profile is CRITICAL."""

    def test_absent_profile_warns(self, tmp_path, monkeypatch, capsys):
        _healthy_env(tmp_path)
        _stub_probe(monkeypatch)
        rc, _, payload = _run(capsys, ["doctor", "--root", str(tmp_path)])
        assert rc == 0
        prof = _check(payload, "client profile")
        assert prof["status"] == "warn"
        assert "transport defaults in use" in prof["detail"]
        assert "advisory" in prof["detail"]

    def test_coherent_profile_passes(self, tmp_path, monkeypatch, capsys):
        _healthy_env(tmp_path)
        _stub_probe(monkeypatch)
        (tmp_path / "data" / "profile.json").write_text(
            ClientProfile().model_dump_json(), encoding="utf-8")
        rc, _, payload = _run(capsys, ["doctor", "--root", str(tmp_path)])
        assert rc == 0
        prof = _check(payload, "client profile")
        assert prof["status"] == "pass"
        assert "coherent" in prof["detail"]

    def test_incoherent_profile_fails_exit_1(
            self, tmp_path, monkeypatch, capsys):
        _healthy_env(tmp_path)
        _stub_probe(monkeypatch)
        # UA says Chrome/136 but sec-ch-ua carries 135 — docs/08 §5 mismatch.
        broken = ClientProfile(
            sec_ch_ua='"Chromium";v="135", "Not_A Brand";v="99", '
                      '"Google Chrome";v="135"')
        (tmp_path / "data" / "profile.json").write_text(
            broken.model_dump_json(), encoding="utf-8")
        rc, _, payload = _run(capsys, ["doctor", "--root", str(tmp_path)])
        assert rc == 1
        prof = _check(payload, "client profile")
        assert prof["status"] == "fail"
        assert "mismatch" in prof["detail"]

    def test_unreadable_profile_fails(self, tmp_path, monkeypatch, capsys):
        _healthy_env(tmp_path)
        _stub_probe(monkeypatch)
        (tmp_path / "data" / "profile.json").write_text(
            "}}not json", encoding="utf-8")
        rc, _, payload = _run(capsys, ["doctor", "--root", str(tmp_path)])
        assert rc == 1
        assert _check(payload, "client profile")["status"] == "fail"


class TestDoctorImpersonate:
    """The impersonate check: the local refused-port probe result plus the
    profile's pinned target, with the offline claim in the line itself."""

    def test_real_probe_passes_offline(self, tmp_path, capsys):
        """The REAL resolve_impersonate (127.0.0.1:9 refused-port probe,
        docs/12 §3): local traffic only, must pass on any healthy build."""
        _healthy_env(tmp_path)
        rc, _, payload = _run(capsys, ["doctor", "--root", str(tmp_path)])
        assert rc == 0
        imp = _check(payload, "impersonate")
        assert imp["status"] == "pass"
        assert "127.0.0.1:9" in imp["detail"]
        assert "offline" in imp["detail"]

    def test_rejected_target_fails(self, tmp_path, monkeypatch, capsys):
        _healthy_env(tmp_path)
        monkeypatch.setattr(
            doctor_mod, "resolve_impersonate",
            lambda preferred: (_ for _ in ()).throw(FingerprintRejectedError(
                "no supported curl_cffi impersonation target found")))
        rc, _, payload = _run(capsys, ["doctor", "--root", str(tmp_path)])
        assert rc == 1
        imp = _check(payload, "impersonate")
        assert imp["status"] == "fail"
        assert imp["critical"] is True

    def test_fallback_divergence_from_profile_pin_warns(
            self, tmp_path, monkeypatch, capsys):
        _healthy_env(tmp_path)
        _stub_probe(monkeypatch, result="chrome131")
        rc, _, payload = _run(capsys, ["doctor", "--root", str(tmp_path)])
        assert rc == 0
        imp = _check(payload, "impersonate")
        assert imp["status"] == "warn"
        assert "chrome136" in imp["detail"]


class TestDoctorState:
    """state/ must exist and be WRITABLE (CRITICAL); a corrupt governor
    state file is a WARN — fail-soft by design (governor.py)."""

    def test_state_dir_missing_fails_exit_1(
            self, tmp_path, monkeypatch, capsys):
        _healthy_env(tmp_path)
        _stub_probe(monkeypatch)
        (tmp_path / "state").rmdir()
        rc, _, payload = _run(capsys, ["doctor", "--root", str(tmp_path)])
        assert rc == 1
        state = _check(payload, "state dir")
        assert state["status"] == "fail"
        assert "missing" in state["detail"]

    def test_governor_state_corrupt_warns_not_fails(
            self, tmp_path, monkeypatch, capsys):
        _healthy_env(tmp_path)
        _stub_probe(monkeypatch)
        (tmp_path / "state" / "governor_state.json").write_text(
            "{corrupt", encoding="utf-8")
        rc, _, payload = _run(capsys, ["doctor", "--root", str(tmp_path)])
        assert rc == 0
        state = _check(payload, "state dir")
        assert state["status"] == "warn"
        assert "fail-soft" in state["detail"]

    def test_probe_function_reports_write_failure(
            self, tmp_path, monkeypatch):
        """Unit probe of the writability check: a mocked Path.write_text
        raising OSError — the portable substitute for a read-only
        filesystem, which is not cross-OS testable."""
        state = tmp_path / "state"
        state.mkdir()

        def _boom(self, data, encoding=None, errors=None, newline=None):
            raise OSError("read-only filesystem (mock)")

        monkeypatch.setattr(Path, "write_text", _boom)
        problem = _probe_state_writable(state)
        assert problem is not None
        assert "read-only filesystem" in problem

    def test_probe_function_clean_when_writable(self, tmp_path):
        state = tmp_path / "state"
        state.mkdir()
        assert _probe_state_writable(state) is None
        # the probe file is removed immediately — doctor never leaves state
        assert not (state / ".doctor-probe").exists()

    def test_unwritable_state_dir_fails_exit_1(
            self, tmp_path, monkeypatch, capsys):
        _healthy_env(tmp_path)
        _stub_probe(monkeypatch)

        def _boom(self, data, encoding=None, errors=None, newline=None):
            raise OSError("read-only filesystem (mock)")

        monkeypatch.setattr(Path, "write_text", _boom)
        rc, _, payload = _run(capsys, ["doctor", "--root", str(tmp_path)])
        assert rc == 1
        state = _check(payload, "state dir")
        assert state["status"] == "fail"
        assert "not writable" in state["detail"]


class TestDoctorVersion:
    """Version is informational: never fails the run, reports the resolved
    version and — where the tree ships pyproject.toml — the declared
    comparison."""

    def test_version_informational_without_pyproject(
            self, tmp_path, monkeypatch, capsys):
        _healthy_env(tmp_path)
        _stub_probe(monkeypatch)
        rc, _, payload = _run(capsys, ["doctor", "--root", str(tmp_path)])
        assert rc == 0
        ver = _check(payload, "version")
        assert ver["status"] == "pass"
        assert ver["critical"] is False

    def test_version_matches_pyproject_declared(
            self, tmp_path, monkeypatch, capsys):
        _healthy_env(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            f'[project]\nname = "fbk"\nversion = "{_VERSION}"\n',
            encoding="utf-8")
        _stub_probe(monkeypatch)
        rc, _, payload = _run(capsys, ["doctor", "--root", str(tmp_path)])
        assert rc == 0
        ver = _check(payload, "version")
        assert ver["status"] == "pass"
        assert "matches the pyproject declaration" in ver["detail"]


class TestDoctorWiring:
    """doctor parses on its own register() seam with the common flag set
    (the orchestrator wires it into app.py; these tests never use
    build_parser)."""

    def test_doctor_parses_with_common_flags(self):
        args = _parse(["doctor", "--root", "X", "--json"])
        assert args.command == "doctor"
        assert args.as_json is True
        assert callable(args.fn)

    def test_doctor_accepts_cookies_flag(self):
        args = _parse(["doctor", "--cookies", "C:\\jar\\cookies.txt"])
        assert callable(args.fn)

    def test_doctor_is_a_leaf_no_children(self, capsys):
        with pytest.raises(SystemExit) as ei:
            _parse(["doctor", "bogus"])
        assert ei.value.code == 2
