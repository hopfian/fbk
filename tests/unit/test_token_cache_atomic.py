"""TokenCache atomic save discipline (just-landed fix, wave 2).

save() must land via temp file + ``os.replace``: a torn ``*.json.tmp``
from a crashed save never survives as a stale sibling (the next save
replaces it), and the final cache file always parses. A stale .tmp is
garbage on disk; a torn FINAL file is a silent cache miss on every
subsequent launch.

Complements test_token_cache.py (TTL/load semantics) and
test_empty_dtsg_semantics.py (the empty-token seam). Unit (offline):
every file lives in tmp_path; no session, no network.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import SecretStr

from auth.bootstrap import Bootstrap
from auth.state import LoginState
from token_cache import TokenCache

T0 = 1_789_723_000.0


def make_bootstrap(dtsg: str = "NAfATOMIC:1:1") -> Bootstrap:
    return Bootstrap(state=LoginState.LOGGED_IN, fb_dtsg=SecretStr(dtsg),
                     lsd=SecretStr("LSD"), user_id="1")


class TestAtomicSave:
    """Pins the temp+replace discipline: no .tmp sibling survives a save,
    and a stale one from a previous crash is cleaned out by the next."""

    def test_save_leaves_no_tmp_sibling(self, tmp_path):
        cache = TokenCache(tmp_path / "tokens.json")
        cache.save(make_bootstrap())
        assert list(tmp_path.glob("*.tmp")) == []
        assert (tmp_path / "tokens.json").is_file()

    def test_final_file_parses_and_loads(self, tmp_path):
        cache = TokenCache(tmp_path / "tokens.json")
        cache.save(make_bootstrap())
        raw = json.loads((tmp_path / "tokens.json").read_text(encoding="utf-8"))
        assert raw["fb_dtsg"] == "NAfATOMIC:1:1"
        entry = cache.load(now=T0 + 1)
        assert entry is not None
        assert entry.dtsg() == "NAfATOMIC:1:1"

    def test_stale_tmp_from_a_crashed_save_is_replaced(self, tmp_path):
        """A previous save killed mid-write left garbage behind: the next
        save must overwrite it via replace, not stack a second one."""
        stale = tmp_path / "tokens.json.tmp"
        stale.write_text('{"fb_dtsg": "torn-write-from-crashed-sa',
                         encoding="utf-8")
        cache = TokenCache(tmp_path / "tokens.json")
        cache.save(make_bootstrap("NAfFRESH:1:2"))
        assert list(tmp_path.glob("*.tmp")) == []  # the stale twin is gone
        entry = cache.load(now=T0 + 1)
        assert entry is not None
        assert entry.dtsg() == "NAfFRESH:1:2"  # the fresh write won

    def test_second_save_replaces_previous_entry(self, tmp_path):
        cache = TokenCache(tmp_path / "tokens.json")
        cache.save(make_bootstrap("NAfOLD:1:1"))
        cache.save(make_bootstrap("NAfNEW:1:9"))
        assert list(tmp_path.glob("*.tmp")) == []
        entry = cache.load(now=T0 + 1)
        assert entry is not None
        assert entry.dtsg() == "NAfNEW:1:9"  # never a blend of both writes

    def test_save_creates_missing_parent_directory(self, tmp_path):
        """The state dir may not exist on a fresh checkout — save lands
        atomically anyway (mkdir parents + replace)."""
        cache = TokenCache(tmp_path / "nested" / "state" / "tokens.json")
        cache.save(make_bootstrap())
        assert list(tmp_path.rglob("*.tmp")) == []
        assert cache.load(now=T0 + 1) is not None
