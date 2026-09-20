"""Offline unit tests for the profile + settings surfaces
(docs/02-endpoint-surface-map.md §2.5/§2.12, docs/15 §P3 privacy/composer
ground truth). Everything runs against StubSession with `_fetch`
monkeypatched — no network, no cookie access."""
from __future__ import annotations

import json
import uuid

from fakes import ASSETS, StubSession

import constants as C
from domain.common import Privacy
from graphql.registry import DocIdRegistry
from surfaces.profile import (
    DEFAULT_PROFILE_DOC_ID,
    DEFAULT_PROFILE_QUERY,
    DEFAULT_PROFILE_VARIABLES,
    ProfileService,
)
from surfaces.settings import (
    DEFAULT_SETTINGS_CATEGORIES_VARIABLES,
    DEFAULT_SETTINGS_ROOT_VARIABLES,
    PRIVACY_SAVE_MUTATION,
    SETTINGS_CATEGORIES_DOC_ID,
    SETTINGS_CATEGORIES_QUERY,
    SETTINGS_ROOT_DOC_ID,
    SETTINGS_ROOT_QUERY,
    SettingsService,
)

UID = "12345678901234"


def craft_preload_html(*blocks: tuple[str, str, dict]) -> str:
    """Canned Comet page HTML embedding SSR preload registrations in the
    live wire format (docs/15 §P2-2): {"actorID":…,"preloaderID":
    "adp_<Name>RelayPreloader_<hex>","queryID":…,"variables":{…},
    "queryName":…}."""
    parts = []
    for query_name, doc_id, variables in blocks:
        block = {
            "actorID": UID,
            "preloaderID": f"adp_{query_name}RelayPreloader_deadbeef1234abcd",
            "queryID": doc_id,
            "variables": variables,
            "queryName": query_name,
        }
        parts.append(json.dumps(block, separators=(",", ":")))
    return "<html><head></head><body>" + "".join(parts) + "</body></html>"


def stub_fetch(monkeypatch, service, html: str) -> list[str]:
    """Monkeypatch a service's `_fetch`; return the recorded URLs."""
    urls: list[str] = []

    def fake(url: str) -> str:
        urls.append(url)
        return html

    monkeypatch.setattr(service, "_fetch", fake)
    return urls


class TestProfileSurface:
    """Pins ProfileService.me(): preload replay with graceful fallbacks at
    every failure seam (no preloads, fetch failure, replay rejection)."""

    def test_me_from_preload_replay(self, monkeypatch):
        html = craft_preload_html(
            (DEFAULT_PROFILE_QUERY, DEFAULT_PROFILE_DOC_ID,
             DEFAULT_PROFILE_VARIABLES))
        session = StubSession({
            DEFAULT_PROFILE_QUERY: {"data": {"profile": {"id": UID,
                                                         "name": "Test"}}},
        })
        service = ProfileService(session)
        urls = stub_fetch(monkeypatch, service, html)
        profile = service.me()

        assert urls == ["https://www.facebook.com/me"]
        assert profile.user.id == UID
        assert profile.user.name == "Test"
        assert profile.user.is_self is True
        assert profile.raw == {"profile": ["id", "name"]}
        assert len(session.graphql.calls) == 1
        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == DEFAULT_PROFILE_QUERY
        assert doc_id == DEFAULT_PROFILE_DOC_ID
        assert variables == DEFAULT_PROFILE_VARIABLES

    def test_me_fallback_when_no_preloads(self, monkeypatch):
        session = StubSession({})
        service = ProfileService(session)
        stub_fetch(monkeypatch, service, "<html><body>no preloads</body></html>")
        profile = service.me()

        assert profile.user.id == UID
        assert profile.user.name == "Test User"
        assert profile.user.is_self is True
        assert profile.raw == {}
        assert session.graphql.calls == []

    def test_me_fallback_when_fetch_fails(self, monkeypatch):
        session = StubSession({})
        service = ProfileService(session)

        def boom(url: str) -> str:
            raise RuntimeError("edge soft-block")

        monkeypatch.setattr(service, "_fetch", boom)
        profile = service.me()

        assert profile.user.id == UID
        assert profile.user.name == "Test User"
        assert profile.raw == {}

    def test_me_survives_failed_replay(self, monkeypatch):
        html = craft_preload_html(
            (DEFAULT_PROFILE_QUERY, DEFAULT_PROFILE_DOC_ID,
             DEFAULT_PROFILE_VARIABLES))
        session = StubSession({
            DEFAULT_PROFILE_QUERY: RuntimeError("doc_id rejected"),
        })
        service = ProfileService(session)
        stub_fetch(monkeypatch, service, html)
        profile = service.me()

        assert profile.user.id == UID
        assert profile.user.name == "Test User"
        assert profile.raw == {}


class TestSettingsSurface:
    """Pins the settings reads and the privacy/reauth/reset mutations —
    secrets confined to wire variables, never responses."""

    def test_show_trimmed_payloads(self, monkeypatch):
        html = craft_preload_html(
            (SETTINGS_ROOT_QUERY, SETTINGS_ROOT_DOC_ID,
             DEFAULT_SETTINGS_ROOT_VARIABLES),
            (SETTINGS_CATEGORIES_QUERY, SETTINGS_CATEGORIES_DOC_ID,
             DEFAULT_SETTINGS_CATEGORIES_VARIABLES),
        )
        session = StubSession({
            SETTINGS_ROOT_QUERY: {"data": {
                "xfb_unified_settings": {"category": {"title": "Settings"},
                                         "navigation": {}, "search": {}},
                "viewer": {"actor": {"id": UID}},
            }},
            SETTINGS_CATEGORIES_QUERY: {"data": {
                "xfb_unified_settings": {"category": {"children": [
                    {"title": "Your friends"}]}},
            }},
        })
        service = SettingsService(session)
        urls = stub_fetch(monkeypatch, service, html)
        shown = service.show()

        assert urls == ["https://www.facebook.com/settings/"]
        assert shown["root"] == {
            "data": {"xfb_unified_settings": ["category", "navigation", "search"],
                     "viewer": ["actor"]},
        }
        assert shown["categories"] == {
            "data": {"xfb_unified_settings": ["category"]},
        }
        assert [(c[0], c[1]) for c in session.graphql.calls] == [
            (SETTINGS_ROOT_QUERY, SETTINGS_ROOT_DOC_ID),
            (SETTINGS_CATEGORIES_QUERY, SETTINGS_CATEGORIES_DOC_ID),
        ]
        assert session.graphql.calls[0][2] == DEFAULT_SETTINGS_ROOT_VARIABLES
        assert session.graphql.calls[1][2] == DEFAULT_SETTINGS_CATEGORIES_VARIABLES

    def test_set_default_privacy_private(self):
        session = StubSession({
            PRIVACY_SAVE_MUTATION: {"data": {"saved": True}},
        })
        service = SettingsService(session)
        response = service.set_default_privacy(Privacy.PRIVATE)

        assert response == {"data": {"saved": True}}
        assert len(session.graphql.calls) == 1
        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == PRIVACY_SAVE_MUTATION
        assert doc_id == "27802157519437974"
        assert variables["input"]["privacy_row_input"]["base_state"] == "SELF"
        assert variables["input"]["actor_id"] == UID
        cmid = variables["input"]["client_mutation_id"]
        assert cmid != "1"
        uuid.UUID(cmid)
        assert variables["__relay_internal__pv__CometUFIShareActionMigrationrelayprovider"] is True
        assert variables["__relay_internal__pv__CometUFISingleLineUFIrelayprovider"] is True
        assert variables["input"]["privacy_row_input"]["tag_expansion_state"] == "TAGGEES"

    def test_verify_password_lands_only_in_variables(self):
        secret = "s3cret-hunter2"
        session = StubSession({
            "CometPasswordReauthenticationMutation": {
                "data": {"comet_password_reauthentication": {
                    "reauth_is_successful": True}},
            },
        })
        service = SettingsService(session)
        response = service.verify_password(secret)

        assert secret not in json.dumps(response)
        assert len(session.graphql.calls) == 1
        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == "CometPasswordReauthenticationMutation"
        assert doc_id == "28435732132699112"
        assert variables["input"]["password"]["sensitive_string_value"] == secret
        cmid = variables["input"]["client_mutation_id"]
        uuid.UUID(cmid)
        assert cmid

        second = service.verify_password(secret)
        cmid2 = session.graphql.calls[1][2]["input"]["client_mutation_id"]
        assert cmid2 != cmid
        assert secret not in json.dumps(second)

    def test_send_reset_link_fresh_client_mutation_id(self):
        session = StubSession({
            "useFXSettingsSendPasswordResetLinkForViewerMutation": {
                "data": {"xfb_send_password_reset_link_for_viewer": {
                    "success": True}},
            },
        })
        service = SettingsService(session)
        response = service.send_reset_link()

        assert response["data"]["xfb_send_password_reset_link_for_viewer"]["success"] is True
        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == "useFXSettingsSendPasswordResetLinkForViewerMutation"
        assert doc_id == "23901062569500160"
        assert set(variables) == {"client_mutation_id"}
        uuid.UUID(variables["client_mutation_id"])

    def test_registry_contains_all_four_doc_ids(self):
        registry = DocIdRegistry.from_assets(ASSETS)
        expected = {
            "CometSettingsRootUSFNewCometQuery": "27695179403496589",
            "SettingsCategoryLayoutUSFNewCometQuery": "27598638776468742",
            "CometPasswordReauthenticationMutation": "28435732132699112",
            "useFXSettingsSendPasswordResetLinkForViewerMutation":
                "23901062569500160",
        }
        for name, doc_id in expected.items():
            assert registry.doc_id(name) == doc_id
        assert C.KNOWN_MUTATIONS["CometPrivacySelectorSavePrivacyMutation"] \
            == "27802157519437974"
