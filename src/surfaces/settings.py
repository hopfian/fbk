"""Settings surface service (docs/02 §2.12, docs/15-live-calibration-findings.md §P3).

ARCHITECTURE:

  Reads follow the any-surface-page law (docs/15 §P3-5): GET /settings/
  (and its privacy tab), harvest the SSR preload registrations, replay
  them verbatim. The default-privacy write replays the VERIFIED captured
  composer template (assets/captured_composer.json) — the same
  verbatim-replay invariant the feed surface enforces, with only the
  privacy enum, actor id and nonce substituted. Password mutations
  ship schema-decoded hook shapes. All traffic flows through
  Session/GraphQLClient, so the service is fully offline-testable
  against StubSession (tests/fakes.py).

CALIBRATION NOTES — ground truth (all live-verified 2026-09):
  * settings queries: CometSettingsRootUSFNewCometQuery (27695179403496589)
    preloads on /settings/; SettingsCategoryLayoutUSFNewCometQuery
    (27598638776468742) preloads on /settings/privacy/ — replayed verbatim.
  * default-privacy save: CometPrivacySelectorSavePrivacyMutation
    (KNOWN_MUTATIONS) — the captured composer template is the VERIFIED
    variable shape (docs/15 §P3 / assets/captured_composer.json);
    base_state is the Privacy enum value ("EVERYONE"/"FRIENDS"/"SELF").
  * password mutations: direct password CHANGE is not on the www GraphQL
    surface (it lives in Meta Accounts Center) — the honest web commands are
    verify-password (CometPasswordReauthenticationMutation) and
    send-reset-link (useFXSettingsSendPasswordResetLinkForViewerMutation).

USER-DOC ANCHOR: cli/docs/06-reference-people.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import copy
from typing import Any
from uuid import uuid4

import constants as C
from domain.common import Privacy

from .base import Surface

SETTINGS_URL = "https://www.facebook.com/settings/"
SETTINGS_PRIVACY_URL = "https://www.facebook.com/settings/privacy/"

SETTINGS_ROOT_QUERY = "CometSettingsRootUSFNewCometQuery"
SETTINGS_ROOT_DOC_ID = "27695179403496589"
SETTINGS_CATEGORIES_QUERY = "SettingsCategoryLayoutUSFNewCometQuery"
SETTINGS_CATEGORIES_DOC_ID = "27598638776468742"

# Live-captured SSR preloader fallbacks (docs/15 §P2-2), used when the
# settings pages carry no harvestable preload (e.g. a stubbed session).
DEFAULT_SETTINGS_ROOT_VARIABLES: dict[str, Any] = {
    "audienceSelectorEntryPoint": "COMET_USF_SETTING",
    "entryPoint": "SETTINGS",
    "groupID": None,
    "nodeID": "FACEBOOK_SETTINGS",
    "scale": 2,
    "settingTreeType": "FACEBOOK_SETTINGS",
}
DEFAULT_SETTINGS_CATEGORIES_VARIABLES: dict[str, Any] = {
    "audienceSelectorEntryPoint": "COMET_USF_SETTING",
    "entryPoint": "privacy_checkup",
    "include_breadcrumbs": True,
    "nodeID": "how_people_find_and_contact_you",
}

PRIVACY_SAVE_MUTATION = "CometPrivacySelectorSavePrivacyMutation"
# The live-captured renderer scope id (b64 'privacy_scope_renderer:{"id":…}'
# from the settings privacy row — docs/15 §P3); opaque, replayed verbatim.
PRIVACY_WRITE_ID = "cHJpdmFjeV9zY29wZV9yZW5kZXJlcjp7ImlkIjo4Nzg3NjcwNzMzfQ"
PRIVACY_RENDER_LOCATION = "COMET_RESOLVE_DEFAULT_FEED_AND_REELS_PRIVACY_CONFLICT"

# The VERIFIED live-captured variable shape (assets/captured_composer.json,
# docs/15 §P3). The captured actor_id / client_mutation_id placeholders are
# substituted per call; everything else — the renderer scope, the
# tag_expansion_state, the relay provider gates — re-rides verbatim.
PRIVACY_SAVE_VARIABLES: dict[str, Any] = {
    "input": {
        "privacy_mutation_token": None,
        "privacy_row_input": {
            "allow": [],
            "base_state": "FRIENDS",
            "deny": [],
            "tag_expansion_state": "TAGGEES",
        },
        "privacy_write_id": PRIVACY_WRITE_ID,
        "render_location": PRIVACY_RENDER_LOCATION,
        "actor_id": "12345678901234",
        "client_mutation_id": "1",
    },
    "privacySelectorRenderLocation": PRIVACY_RENDER_LOCATION,
    "scale": 1,
    "storyRenderLocation": None,
    "tags": None,
    "__relay_internal__pv__CometUFIShareActionMigrationrelayprovider": True,
    "__relay_internal__pv__CometUFISingleLineUFIrelayprovider": True,
}

PASSWORD_REAUTH_MUTATION = "CometPasswordReauthenticationMutation"
PASSWORD_REAUTH_VARIABLES_TEMPLATE: dict[str, Any] = {
    # schema-decoded 2026-09 (rsrc bundle aBPr7NfprtF.js / F8fvAMgi7Z.js):
    # LocalArgument "input"; request builder passes
    # {input: {password: {sensitive_string_value: <pw>}}}; selection
    # comet_password_reauthentication {reauth_is_successful}
    "input": {
        "password": {"sensitive_string_value": None},
        "client_mutation_id": None,
    },
}

SEND_RESET_LINK_MUTATION = "useFXSettingsSendPasswordResetLinkForViewerMutation"
SEND_RESET_LINK_VARIABLES_TEMPLATE: dict[str, Any] = {
    # schema-decoded 2026-09 (rsrc bundle WZAM9srVby7.js):
    # LocalArgument "client_mutation_id"; hook commits
    # {variables: {client_mutation_id: uuidv4()}}; selection
    # xfb_send_password_reset_link_for_viewer {success, response_message}
    "client_mutation_id": None,
}


def _trim(node: Any, levels: int = 2) -> Any:
    """Keep the top `levels` of a payload; deeper dicts collapse to their key
    lists, lists to a length fingerprint (journal/emit-safe views)."""
    def cut(n: Any, depth: int) -> Any:
        if isinstance(n, dict):
            if depth >= levels:
                return sorted(n.keys())
            return {k: cut(v, depth + 1) for k, v in n.items()}
        if isinstance(n, list):
            if depth >= levels:
                return f"<list:{len(n)}>"
            return [cut(v, depth + 1) for v in n[:4]]
        return n

    return cut(node, 0)


class SettingsService(Surface):
    """Account-settings reads + the live-verified settings mutations
    (docs/02 §2.12)."""

    # ------------------------------------------------------------------- reads
    def show(self) -> dict[str, Any]:
        """Replay both settings queries with their verbatim page variables.

        The category query lives on the privacy tab of the settings
        surface (live-verified 2026-09), so the categories preload is
        harvested from /settings/privacy/ when /settings/ does not carry
        it (docs/15 §P2-2 verbatim preload methodology).

        Returns:
            {"root": <trimmed root payload>, "categories": <trimmed
            categories payload>} — top 2 levels kept, deeper dicts
            collapsed to key lists and lists to length fingerprints
            (journal/emit-safe views via _trim).
        """
        entries = self._preloads(SETTINGS_URL)
        root_entry = self._find(entries, SETTINGS_ROOT_QUERY)
        cat_entry = self._find(entries, SETTINGS_CATEGORIES_QUERY)
        if cat_entry is None:
            cat_entry = self._find(self._preloads(SETTINGS_PRIVACY_URL),
                                   SETTINGS_CATEGORIES_QUERY)

        if root_entry is not None:
            root_doc, root_vars = root_entry.doc_id, root_entry.variables
        else:
            root_doc = self.doc_id(SETTINGS_ROOT_QUERY)
            root_vars = copy.deepcopy(DEFAULT_SETTINGS_ROOT_VARIABLES)
        if cat_entry is not None:
            cat_doc, cat_vars = cat_entry.doc_id, cat_entry.variables
        else:
            cat_doc = self.doc_id(SETTINGS_CATEGORIES_QUERY)
            cat_vars = copy.deepcopy(DEFAULT_SETTINGS_CATEGORIES_VARIABLES)

        root_payload = self.client.call(SETTINGS_ROOT_QUERY, root_doc, root_vars)
        categories_payload = self.client.call(SETTINGS_CATEGORIES_QUERY,
                                              cat_doc, cat_vars)
        return {
            "root": _trim(root_payload, 2),
            "categories": _trim(categories_payload, 2),
        }

    # -------------------------------------------------------------- mutations
    def set_default_privacy(self, privacy: Privacy) -> dict[str, Any]:
        """Save the default post privacy (docs/15 §P3 ground truth).

        Deep-copies the VERIFIED captured template (PRIVACY_SAVE_VARIABLES),
        substituting privacy_row_input.base_state with the Privacy enum
        value, the actor_id with the session user, and a fresh
        client_mutation_id; the renderer scope and every other captured
        field re-ride verbatim.

        Args:
            privacy: The new default audience; privacy.value rides
                privacy_row_input.base_state ("EVERYONE"/"FRIENDS"/
                "SELF" — the live-captured string enums).

        Returns:
            The merged CometPrivacySelectorSavePrivacyMutation response.
        """
        variables = copy.deepcopy(PRIVACY_SAVE_VARIABLES)
        variables["input"]["privacy_row_input"]["base_state"] = privacy.value
        variables["input"]["actor_id"] = self.session.user_id()
        variables["input"]["client_mutation_id"] = str(uuid4())
        return self.client.call(PRIVACY_SAVE_MUTATION,
                                C.KNOWN_MUTATIONS[PRIVACY_SAVE_MUTATION],
                                variables)

    def verify_password(self, current_password: str) -> dict[str, Any]:
        """Verify the current password (CometPasswordReauthenticationMutation).

        The password travels ONLY inside the mutation variables
        (input.password.sensitive_string_value — schema-decoded 2026-09);
        it never reaches journal emission, which fingerprints payloads
        rather than logging them (docs/12 §4 redaction policy).

        Args:
            current_password: The plaintext current password; wrapped in
                the schema's sensitive_string_value input member.

        Returns:
            The merged response; comet_password_reauthentication
            .reauth_is_successful carries the verdict.

        Note:
            A failed verification surfaces as a GraphQL error envelope,
            not a False return — treat error-class responses as a
            negative verdict.
        """
        variables = copy.deepcopy(PASSWORD_REAUTH_VARIABLES_TEMPLATE)
        variables["input"]["password"]["sensitive_string_value"] = current_password
        variables["input"]["client_mutation_id"] = str(uuid4())
        return self.client.call(PASSWORD_REAUTH_MUTATION,
                                 self.doc_id(PASSWORD_REAUTH_MUTATION),
                                 variables)

    def send_reset_link(self) -> dict[str, Any]:
        """Trigger a password-reset link email
        (useFXSettingsSendPasswordResetLinkForViewerMutation).

        The mutation's only argument is a fresh client_mutation_id
        (schema-decoded 2026-09); the viewer's email address is bound
        server-side to the session.

        Returns:
            The merged response; xfb_send_password_reset_link_for_viewer
            carries {success, response_message}.
        """
        variables = copy.deepcopy(SEND_RESET_LINK_VARIABLES_TEMPLATE)
        variables["client_mutation_id"] = str(uuid4())
        return self.client.call(SEND_RESET_LINK_MUTATION,
                                self.doc_id(SEND_RESET_LINK_MUTATION),
                                variables)
