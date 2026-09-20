"""``fbk registry diff`` — offline diff of the on-disk registry versions.

Unit (offline): runs against the REAL data/ registries (v2 + v3, the
actual shipped files) and pins the diff's internal consistency — pair
counts reconcile, category sets are disjoint, changed names genuinely
carry different doc_ids across versions. Plus the typed exit-6 path
when v3 is absent (nothing refreshed yet) and the --limit head cap.
"""
from __future__ import annotations

import json
from pathlib import Path

from app import build_parser
from commands.common import run_command

CLI_ROOT = Path(__file__).resolve().parents[2]


def _run(capsys, argv) -> tuple[int, str]:
    args = build_parser().parse_args(argv)
    rc = args.fn(args)
    out = capsys.readouterr().out
    return rc, out


def _pairs(assets_dir: Path, name: str) -> dict[str, str]:
    data = json.loads((assets_dir / name).read_text(encoding="utf-8"))
    return {p["friendly_name"]: p["doc_id"] for p in data["unique_pairs"]}


class TestRegistryDiff:
    """Pins the offline v2-vs-v3 diff on the real shipped registries."""

    def test_wiring_exposes_registry_diff(self):
        args = build_parser().parse_args(["registry", "diff"])
        assert args.command == "registry"
        assert args.registry_command == "diff"
        assert callable(args.fn)

    def test_real_data_diff_is_internally_consistent(self, capsys):
        rc, out = _run(capsys, ["registry", "diff", "--json",
                                "--root", str(CLI_ROOT)])
        assert rc == 0
        payload = json.loads(out)
        assert payload["old"]["source"] == "doc_id_registry_v2.json"
        assert payload["new"]["source"] == "doc_id_registry_v3.json"
        counts = payload["counts"]
        # v3 = v2 - removed + added: the counts must reconcile exactly
        assert (payload["new"]["pairs"]
                == payload["old"]["pairs"] + counts["added"] - counts["removed"])
        added = set(payload["added"])
        removed = set(payload["removed"])
        changed = set(payload["changed"])
        assert not (added & removed) and not (added & changed)
        assert not (removed & changed)
        # against the actual files: every changed name really differs,
        # every added name is genuinely new to v3
        v2 = _pairs(CLI_ROOT / "data", "doc_id_registry_v2.json")
        v3 = _pairs(CLI_ROOT / "data", "doc_id_registry_v3.json")
        assert set(v3) - set(v2) == added
        assert set(v2) - set(v3) == removed
        for name, (old_id, new_id) in payload["changed"].items():
            assert old_id != new_id
            assert v2[name] == old_id and v3[name] == new_id

    def test_human_output_matches_refresh_style(self, capsys):
        rc, out = _run(capsys, ["registry", "diff", "--root", str(CLI_ROOT)])
        assert rc == 0
        assert "old: doc_id_registry_v2.json (" in out
        assert "new: doc_id_registry_v3.json (" in out
        assert "added " in out and "changed " in out and "removed " in out
        # JSON line closes the output (the emit contract)
        json.loads(out.splitlines()[-1])

    def test_limit_caps_the_human_head_per_category(self, capsys):
        rc, plain = _run(capsys, ["registry", "diff", "--root", str(CLI_ROOT)])
        rc2, limited = _run(capsys, ["registry", "diff", "--limit", "1",
                                     "--root", str(CLI_ROOT)])
        assert rc == rc2 == 0
        payload_plain = json.loads(plain.splitlines()[-1])
        payload_limited = json.loads(limited.splitlines()[-1])
        assert len(payload_limited["head"]["added"]) == 1
        assert len(payload_plain["head"]["added"]) == payload_plain["counts"]["added"]

    def test_missing_v3_is_a_typed_registry_miss_exit_6(self, tmp_path, capsys,
                                                         monkeypatch):
        monkeypatch.delenv("FBK_ROOT", raising=False)
        monkeypatch.delenv("FBK_COOKIES", raising=False)
        data = tmp_path / "data"
        data.mkdir()
        # only v2 present: nothing has been refreshed/saved yet
        (data / "doc_id_registry_v2.json").write_text(
            (CLI_ROOT / "data" / "doc_id_registry_v2.json").read_text(encoding="utf-8"),
            encoding="utf-8")
        args = build_parser().parse_args(["registry", "diff",
                                          "--root", str(tmp_path)])
        rc = run_command(args.fn, args)
        err = capsys.readouterr().err
        assert rc == 6
        assert "doc_id_registry_v3.json" in err
