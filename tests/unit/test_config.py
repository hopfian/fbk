"""Config discovery tests.

Unit (offline): root discovery via cookies.txt probing and the
FBK_ROOT / FBK_COOKIES env overrides (monkeypatched); tmp_path supplies
the synthetic roots. No network, no live session.
"""


from config import Config


class TestConfig:
    """Pins root discovery via cookies.txt probing and the FBK_ROOT /
    FBK_COOKIES env overrides."""

    def test_discovers_project_root(self, monkeypatch):
        monkeypatch.delenv("FBK_ROOT", raising=False)
        cfg = Config.discover()
        # src/config.py roots at cli/ (editable) or the flat install
        # dir (wheel) — pyproject.toml marks the package root.
        assert (cfg.root / "pyproject.toml").exists() or (cfg.root / "data").is_dir()

    def test_env_root_override(self, tmp_path, monkeypatch):
        (tmp_path / "cookies.txt").write_text("# stub\n", encoding="utf-8")
        monkeypatch.setenv("FBK_ROOT", str(tmp_path))
        cfg = Config.discover()
        assert cfg.root == tmp_path.resolve()
        assert cfg.cookies_path == tmp_path / "cookies.txt"

    def test_cookies_override_wins(self, tmp_path, monkeypatch):
        alt = tmp_path / "other-cookies.txt"
        alt.write_text("# stub\n", encoding="utf-8")
        monkeypatch.setenv("FBK_COOKIES", str(alt))
        cfg = Config.discover(tmp_path)
        assert cfg.cookies_path == alt

    def test_journal_file_paths(self, tmp_path):
        cfg = Config.discover(tmp_path)
        assert cfg.journal_file("run").parent == cfg.state_dir
        assert cfg.journal_file("run").name == "run.jsonl"
