"""``fbk config show`` — offline environment introspection.

Unit (offline): drives the REAL app parser (the exact object the fbk
entrypoint dispatches) against tmp_path roots via ``--root`` (the
CLI-flag equivalent of FBK_ROOT) and via the FBK_ROOT env override; no
network, no cookies.txt — a missing jar is a REPORTED fact here, never
a failure.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import constants as C
from app import build_parser

CLI_ROOT = Path(__file__).resolve().parents[2]


def _clear_env(monkeypatch) -> None:
    monkeypatch.delenv("FBK_ROOT", raising=False)
    monkeypatch.delenv("FBK_COOKIES", raising=False)
    monkeypatch.delenv("FBK_IMPERSONATE", raising=False)


def _run(capsys, argv) -> tuple[int, str, dict]:
    """Drive ``config show`` end to end; return (rc, stdout, payload).

    The payload parses from the WHOLE output in --json mode (indented,
    machine-only) and from the trailing compact line in human mode (the
    emit contract).
    """
    args = build_parser().parse_args(argv)
    rc = args.fn(args)
    out = capsys.readouterr().out
    try:
        return rc, out, json.loads(out)
    except json.JSONDecodeError:
        return rc, out, json.loads(out.splitlines()[-1])


class TestConfigShow:
    """Pins the resolved-field payload of ``config show`` on a synthetic
    root: paths, identity, wire shape, governor summary."""

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        _clear_env(monkeypatch)

    def test_renders_all_resolved_fields(self, tmp_path, capsys):
        rc, _, payload = _run(capsys, ["config", "show", "--root", str(tmp_path)])
        assert rc == 0
        root = tmp_path.resolve()
        assert payload["root"] == str(root)
        assert payload["data_dir"] == str(root / "data")
        assert payload["state_dir"] == str(root / "state")
        assert payload["cookies_path"] == str(root / "cookies.txt")
        assert payload["impersonate"] == C.IMPERSONATE_DEFAULT
        assert payload["timeout"] == 30.0
        assert payload["graphql_endpoint"] == C.GRAPHQL_ENDPOINT

    def test_missing_inputs_are_reported_not_failed(self, tmp_path, capsys):
        """The whole point of the command: it works with NO cookies.txt —
        absence is payload data (cookies_present/profile_present false),
        never an error exit."""
        rc, _, payload = _run(capsys, ["config", "show", "--root", str(tmp_path)])
        assert rc == 0
        assert payload["cookies_present"] is False
        assert payload["profile_path"] is None
        assert payload["profile_present"] is False

    def test_present_inputs_flagged(self, tmp_path, capsys):
        (tmp_path / "cookies.txt").write_text("# stub\n", encoding="utf-8")
        data = tmp_path / "data"
        data.mkdir()
        (data / "profile.json").write_text("{}", encoding="utf-8")
        rc, _, payload = _run(capsys, ["config", "show", "--root", str(tmp_path)])
        assert rc == 0
        assert payload["cookies_present"] is True
        assert payload["profile_present"] is True
        assert payload["profile_path"].endswith("profile.json")

    def test_fbk_root_env_override_is_respected(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("FBK_ROOT", str(tmp_path))
        rc, _, payload = _run(capsys, ["config", "show"])
        assert rc == 0
        assert payload["root"] == str(tmp_path.resolve())

    def test_governor_summary_embedded(self, tmp_path, capsys):
        """The caps block is default_governor().status() verbatim — assert
        the governor vocabulary's keys, not duplicated shapes."""
        rc, _, payload = _run(capsys, ["config", "show", "--root", str(tmp_path)])
        assert rc == 0
        for key in ("enabled", "hour_count", "hourly_cap", "day_count",
                    "daily_cap", "mutation_daily_cap"):
            assert key in payload["governor"]

    def test_human_mode_renders_every_field_then_json(self, tmp_path, capsys):
        rc, out, payload = _run(capsys, ["config", "show", "--root", str(tmp_path)])
        assert rc == 0
        for fragment in (payload["root"], payload["data_dir"],
                         payload["state_dir"], payload["cookies_path"],
                         C.IMPERSONATE_DEFAULT, C.GRAPHQL_ENDPOINT, "governor"):
            assert fragment in out
        # human lines + the trailing compact JSON line (the emit contract)
        assert out.count("\n") > 1

    def test_json_mode_is_machine_only(self, tmp_path, capsys):
        rc, out, _ = _run(capsys, ["config", "show", "--root", str(tmp_path),
                                   "--json"])
        assert rc == 0
        # the WHOLE output parses as JSON only in --json mode — human
        # mode would prefix decoration lines
        assert json.loads(out)["root"] == str(tmp_path.resolve())
        assert "root             :" not in out  # no human decoration


class TestIntrospectionWiring:
    """Pins the new offline-introspection families on the real app
    parser: config/cookies exist with their children, and both require
    a child (argparse exit 2, house convention for required groups)."""

    @pytest.mark.parametrize("argv,child", (
        (["config", "show"], "show"),
        (["cookies", "inspect"], "inspect"),
        (["registry", "diff"], "diff"),
    ))
    def test_families_parse_with_their_children(self, argv, child):
        args = build_parser().parse_args(argv)
        assert callable(args.fn)
        assert argv[1] == child

    @pytest.mark.parametrize("argv", (["config"], ["cookies"]))
    def test_families_require_a_child(self, capsys, argv):
        with pytest.raises(SystemExit) as ei:
            build_parser().parse_args(argv)
        assert ei.value.code == 2
        assert "required" in capsys.readouterr().err
