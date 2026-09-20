"""Test bootstrap: asset fixtures (real captured wire data) + live-gating.

Every fixture draws from cli/data/ - the actual bytes and payloads captured
during Phases 1-3 (docs/15), so tests validate against the REAL protocol
shapes, not hand-waved approximations.

The cli/ directory is fully self-contained: no file outside cli/ is
referenced by any test or source module.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

# cli/ is the package root (contains pyproject.toml + src/ + tests/)
CLI_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = CLI_ROOT / "data"

# src/ goes on the path (the editable install already does this, but be
# explicit so tests also run from a fresh checkout without install)
sys.path.insert(0, str(CLI_ROOT / "src"))



def pytest_configure(config):
    config.addinivalue_line("markers", "live: needs cookies.txt + network (FBK_LIVE=1)")


def pytest_collection_modifyitems(config, items):
    """Live gate: skip every ``live``-marked item unless FBK_LIVE=1.

    The ``live`` marker is registered in :func:`pytest_configure` above
    (cookies.txt + network required); without the env var the whole
    live plane must collect as skips, never as failures.
    """
    if os.environ.get("FBK_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="live tests gated by FBK_LIVE=1")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


def load_asset(name: str) -> Any:
    """Load a cli/data/ JSON file (raise a clean error when missing)."""
    path = DATA_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"missing captured fixture: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def assets_dir() -> Path:
    """Session-scoped handle on cli/data/ (the real captured wire assets)."""
    return DATA_DIR


@pytest.fixture()
def ws_capture() -> list[dict]:
    """The full-byte Messenger WS capture (cli/data/messenger_ws_full.json)."""
    return load_asset("messenger_ws_full.json")


@pytest.fixture()
def captured_connect(ws_capture) -> bytes:
    """The real client's MQIsdp CONNECT bytes (452B, doc 15 §P3-1)."""
    import base64
    chat = next(s for s in ws_capture
                if s["url"].startswith("wss://edge-chat.facebook.com"))
    return base64.b64decode(next(f["b64"] for f in chat["frames"]
                                 if f["dir"] == "sent" and f["n"] > 400))


@pytest.fixture()
def feed_page1() -> dict:
    """The live-captured feed payload (CometModernHomeFeedQuery replay, P2-2)."""
    return load_asset("feed_page1_sample.json")


@pytest.fixture()
def captured_react_mutations() -> list[dict]:
    """Browser-captured like + remove mutations (docs/15 §P2-3)."""
    return load_asset("captured_mutations.json")["mutations"]


@pytest.fixture()
def captured_composer() -> dict:
    """Browser-captured publish + privacy mutations (docs/15 §P3 ground truth)."""
    return load_asset("captured_composer.json")
