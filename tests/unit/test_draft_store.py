"""src/drafts.py — the CLI-local draft store.

Unit (offline, hermetic): the name-validation security boundary (the
_journal_path traversal matrix from test_journal_commands.py), the
save/list/load/delete lifecycle in tmp_path, atomic writes (no .tmp
sibling survives), the --force overwrite refusal (the journal-export
convention), and loud failure on a corrupt draft file.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from drafts import DraftExistsError, DraftSpec, DraftStore, is_valid_draft_name


def _spec(name: str, text: str = "hello world", **kw: object) -> DraftSpec:
    """A ready-to-save spec; created_at pinned for deterministic asserts."""
    return DraftSpec(name=name, text=text,
                     created_at="2026-09-20T12:00:00+00:00",
                     **kw)  # type: ignore[arg-type]


class TestNameValidation:
    """The security boundary: a draft name is a BARE basename — anything
    path-shaped is refused before any path resolves (the _journal_path
    matrix), so no invocation can touch anything outside state/drafts/."""

    @pytest.mark.parametrize("bad", ("", ".", "..", "../cookies", "sub/dir",
                                     "..\\evil", "evil\\x", "a/b/c"))
    def test_matrix_rejects_path_shaped_names(self, bad: str):
        assert is_valid_draft_name(bad) is False

    @pytest.mark.parametrize("bad", ("", ".", "..", "../cookies", "sub/dir",
                                     "..\\evil"))
    def test_store_methods_refuse_path_shaped_names(self, tmp_path: Path, bad: str):
        store = DraftStore(tmp_path)
        assert store.exists(bad) is False
        assert store.load(bad) is None
        assert store.delete(bad) is False
        with pytest.raises(ValueError):
            store.save(_spec(bad))

    def test_refused_names_never_create_anything(self, tmp_path: Path):
        """No directory, no file, and nothing outside the tmp root."""
        store = DraftStore(tmp_path)
        for bad in ("../cookies", "sub/dir", "..\\evil"):
            with pytest.raises(ValueError):
                store.save(_spec(bad))
        assert not store.dir.exists()
        assert list(tmp_path.rglob("*")) == []

    def test_plain_basenames_are_valid(self):
        for good in ("a", "promo-2026", "My_Post", "v2.final"):
            assert is_valid_draft_name(good) is True


class TestLifecycle:
    """save → exists/load/list → delete, full round-trip under tmp_path."""

    def test_save_then_exists_and_load_round_trips(self, tmp_path: Path):
        store = DraftStore(tmp_path)
        spec = _spec("promo", media=["a.jpg", "b.jpg"], tags=["1:Ann"],
                     ai_label=True, background="7", privacy="public",
                     feeling="f1", activity="ac1", place="pl1")
        path = store.save(spec)
        assert path == tmp_path / "drafts" / "promo.json"
        assert store.exists("promo") is True
        loaded = store.load("promo")
        assert loaded == spec
        assert loaded is not None and loaded.media == ["a.jpg", "b.jpg"]

    def test_on_disk_file_is_plain_json_under_state_drafts(self, tmp_path: Path):
        store = DraftStore(tmp_path)
        store.save(_spec("x", text="line one\nline two"))
        raw = json.loads((tmp_path / "drafts" / "x.json").read_text("utf-8"))
        assert raw["name"] == "x"
        assert raw["text"] == "line one\nline two"

    def test_list_summarizes_name_order_with_heads(self, tmp_path: Path):
        store = DraftStore(tmp_path)
        store.save(_spec("beta", text="second post text\nmore"))
        store.save(_spec("alpha", text="first", media=["m.jpg"]))
        entries = store.list()
        assert [e["name"] for e in entries] == ["alpha", "beta"]
        [alpha, beta] = entries
        assert alpha["text_head"] == "first"
        assert alpha["media_count"] == 1
        assert beta["text_head"] == "second post text"
        assert beta["media_count"] == 0
        assert alpha["created_at"] == "2026-09-20T12:00:00+00:00"

    def test_list_empty_and_missing_dir_are_empty_facts(self, tmp_path: Path):
        store = DraftStore(tmp_path)
        assert store.list() == []
        (tmp_path / "drafts").mkdir()
        assert DraftStore(tmp_path).list() == []

    def test_delete_removes_then_reports_absent(self, tmp_path: Path):
        store = DraftStore(tmp_path)
        store.save(_spec("gone"))
        assert store.delete("gone") is True
        assert store.exists("gone") is False
        assert store.delete("gone") is False
        assert not (tmp_path / "drafts" / "gone.json").exists()

    def test_load_absent_returns_none(self, tmp_path: Path):
        assert DraftStore(tmp_path).load("nope") is None


class TestAtomicWrite:
    """The governor/token-cache atomic pattern: temp + os.replace — a
    crash mid-save can never leave a half-written draft, and no .tmp
    sibling survives a completed save."""

    def test_no_tmp_sibling_survives_save(self, tmp_path: Path):
        store = DraftStore(tmp_path)
        store.save(_spec("atomic"))
        assert not list((tmp_path / "drafts").glob("*.tmp"))
        assert [p.name for p in (tmp_path / "drafts").iterdir()] == ["atomic.json"]


class TestOverwriteRefusal:
    """The journal-export convention: an existing draft is never silently
    overwritten — DraftExistsError is the refusal, force=True the opt-in."""

    def test_save_over_existing_without_force_refuses(self, tmp_path: Path):
        store = DraftStore(tmp_path)
        store.save(_spec("dup", text="original"))
        with pytest.raises(DraftExistsError):
            store.save(_spec("dup", text="clobber"))

    def test_force_overwrites(self, tmp_path: Path):
        store = DraftStore(tmp_path)
        store.save(_spec("dup", text="original"))
        store.save(_spec("dup", text="replaced"), force=True)
        loaded = store.load("dup")
        assert loaded is not None and loaded.text == "replaced"


class TestCorruptFilesStayLoud:
    """A corrupt draft file is disk rot, never a torn append — load and
    list both raise instead of silently treating the draft as absent."""

    def test_load_corrupt_raises(self, tmp_path: Path):
        store = DraftStore(tmp_path)
        store.save(_spec("rotten"))
        (tmp_path / "drafts" / "rotten.json").write_text("{not json", "utf-8")
        with pytest.raises(ValidationError):
            store.load("rotten")

    def test_list_corrupt_raises(self, tmp_path: Path):
        store = DraftStore(tmp_path)
        store.save(_spec("rotten"))
        (tmp_path / "drafts" / "rotten.json").write_text("{not json", "utf-8")
        with pytest.raises(ValidationError):
            store.list()
