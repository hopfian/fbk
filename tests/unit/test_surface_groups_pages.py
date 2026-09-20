"""Offline tests for the GROUPS + PAGES surfaces (docs/02 §2, docs/15 §3).

StubSession-based: every mutation is recorded (never fired), variable
assembly is verified against the schema-decoded 2026-09 templates, doc_ids
against the harvested registry, and responses against canned wire shapes.
Command wiring is exercised through the real argparse parsers with the
session constructor stubbed out.
"""
from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fakes import StubSession

from commands import groups as groups_cmd
from commands import pages as pages_cmd
from config import Config
from graphql.registry import DocIdRegistry
from surfaces.groups import GroupsService
from surfaces.pages import PagesService

ACTOR_ID = "12345678901234"

PAIRS = {
    "useGroupsCometCreateMutation": "37309644325300927",
    "GroupsCometCreateRootQuery": "27148550504773184",
    "GroupsCometCreatePreviewContentQuery": "27741323172155379",
    "GroupCometJoinForumMutation": "28829108476693589",
    "useGroupAddMembersMutation": "26949495548074408",
    "GroupsCometRequestToParticipateMutation": "27259944290349383",
    "AdditionalProfilePlusCreationMutation": "23863457623296585",
    "AdditionalProfilePlusCreationRootQuery": "29413001758315122",
    "AdditionalProfilePlusCreationFormDetailsLinkWhatsappStepRendererQuery":
        "26076087832013617",
    "CometPageLikeCommitMutation": "9647968328590344",
    "CometPageFollowCommitMutation": "29690201327260308",
    "CometPageFollowDialogQuery": "9988731897871900",
}

CREATE_RESPONSE = {"data": {"create_group": {"group": {"id": "123"}}}}
JOIN_RESPONSE = {"data": {"group_join_forum": {"success": True}}}
ADD_MEMBERS_RESPONSE = {"data": {"group_add_member": {"id": "456"}}}
REQUEST_RESPONSE = {"data": {"request_to_participate_group": {"id": "789"}}}
PAGE_CREATE_RESPONSE = {"data": {"additional_profile_plus_create":
                                 {"page": {"id": "777"}}}}
LIKE_RESPONSE = {"data": {"page_like": {"page": {"id": "555",
                                                 "is_viewer_fan": True}}}}
FOLLOW_RESPONSE = {"data": {"actor_subscribe": {"id": "555"}}}


def stub(responses: dict) -> StubSession:
    return StubSession(responses, PAIRS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fbk-test")
    sub = parser.add_subparsers(dest="command", required=True)
    groups_cmd.register(sub)
    pages_cmd.register(sub)
    return parser


# ------------------------------------------------------------------ registry
class TestRegistryPresence:
    """Pins every ground-truth doc_id in the real asset registry and the
    stub session's hermetic pair registry."""

    def test_every_doc_id_is_harvested(self):
        """Every ground-truth doc_id resolves in the real asset registry."""
        registry = DocIdRegistry.from_assets(Config.discover().assets_dir)
        for name, doc_id in PAIRS.items():
            assert registry.doc_id(name) == doc_id

    def test_stub_registry_matches_ground_truth(self):
        session = stub({})
        for name, doc_id in PAIRS.items():
            assert session.registry.doc_id(name) == doc_id


# --------------------------------------------------------------- groups service
class TestGroupsService:
    """Pins the group mutation family's decoded variable assembly: privacy
    enum, member threading, and actor substitution."""

    def test_create_defaults_private(self):
        session = stub({"useGroupsCometCreateMutation": CREATE_RESPONSE})
        response = GroupsService(session).create("Test Group")
        assert response == CREATE_RESPONSE
        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == "useGroupsCometCreateMutation"
        assert doc_id == "37309644325300927"
        assert variables["input"]["name"] == "Test Group"
        assert variables["input"]["privacy"] == "PRIVATE"
        assert variables["input"]["discoverability"] == "ANYONE"
        assert variables["scale"] == 2

    def test_create_visibility_mapping_and_members(self):
        session = stub({"useGroupsCometCreateMutation": CREATE_RESPONSE})
        GroupsService(session).create("Public Group", visibility="PUBLIC",
                                      member_ids=["111", "222"])
        _, _, variables = session.graphql.calls[0]
        assert variables["input"]["privacy"] == "PUBLIC"
        assert variables["input"]["members"] == ["111", "222"]
        assert variables["input"]["bulk_invitee_members"] == ["111", "222"]

    def test_create_visibility_enum_only(self):
        session = stub({"useGroupsCometCreateMutation": CREATE_RESPONSE})
        with pytest.raises(ValueError):
            GroupsService(session).create("X", visibility="HIDDEN")

    def test_join_substitutes_group_and_actor(self):
        session = stub({"GroupCometJoinForumMutation": JOIN_RESPONSE})
        response = GroupsService(session).join("990011")
        assert response == JOIN_RESPONSE
        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == "GroupCometJoinForumMutation"
        assert doc_id == "28829108476693589"
        assert variables["groupID"] == "990011"
        assert variables["input"]["group_id"] == "990011"
        assert variables["input"]["actor_id"] == ACTOR_ID
        assert variables["feedType"] == "DISCUSSION"
        assert variables["renderLocation"] == "group_mall"

    def test_add_members_threads_group_and_actor(self):
        session = stub({"useGroupAddMembersMutation": ADD_MEMBERS_RESPONSE})
        response = GroupsService(session).add_members(
            "990011", ["111", "222"])
        assert response == ADD_MEMBERS_RESPONSE
        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == "useGroupAddMembersMutation"
        assert doc_id == "26949495548074408"
        assert variables["groupID"] == "990011"
        assert variables["input"]["group_id"] == "990011"
        assert variables["input"]["actor_id"] == ACTOR_ID
        assert variables["input"]["user_ids"] == ["111", "222"]
        assert variables["input"]["email_addresses"] == []

    def test_request_to_participate_without_answers(self):
        session = stub({"GroupsCometRequestToParticipateMutation":
                        REQUEST_RESPONSE})
        response = GroupsService(session).request_to_participate("990011")
        assert response == REQUEST_RESPONSE
        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == "GroupsCometRequestToParticipateMutation"
        assert doc_id == "27259944290349383"
        assert variables["input"]["group_id"] == "990011"
        assert variables["input"]["actor_id"] == ACTOR_ID
        assert "answers" not in variables["input"]

    def test_request_to_participate_encodes_answers(self):
        session = stub({"GroupsCometRequestToParticipateMutation":
                        REQUEST_RESPONSE})
        GroupsService(session).request_to_participate(
            "990011", answers={"q1": "researcher", "q2": "tools"})
        _, _, variables = session.graphql.calls[0]
        assert variables["input"]["answers"] == [
            {"answer": "researcher", "question_id": "q1",
             "selected_options": []},
            {"answer": "tools", "question_id": "q2", "selected_options": []},
        ]


# --------------------------------------------------------------- pages service
class TestPagesService:
    """Pins the page mutation family: like/follow id threading and fresh
    per-call uuid client_mutation_id."""

    def test_create_carries_name_and_category(self):
        session = stub({"AdditionalProfilePlusCreationMutation":
                        PAGE_CREATE_RESPONSE})
        response = PagesService(session).create("CLI Page",
                                                category="Public Figure")
        assert response == PAGE_CREATE_RESPONSE
        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == "AdditionalProfilePlusCreationMutation"
        assert doc_id == "23863457623296585"
        assert variables["input"]["name"] == "CLI Page"
        assert variables["input"]["categories"] == ["Public Figure"]
        assert variables["input"]["creation_source"] == "comet"

    def test_create_without_category(self):
        session = stub({"AdditionalProfilePlusCreationMutation":
                        PAGE_CREATE_RESPONSE})
        PagesService(session).create("Bare Page")
        _, _, variables = session.graphql.calls[0]
        assert variables["input"]["categories"] == []

    def test_like_threads_page_id(self):
        session = stub({"CometPageLikeCommitMutation": LIKE_RESPONSE})
        response = PagesService(session).like("555")
        assert response == LIKE_RESPONSE
        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == "CometPageLikeCommitMutation"
        assert doc_id == "9647968328590344"
        assert variables["input"]["page_id"] == "555"
        assert variables["input"]["actor_id"] == ACTOR_ID

    def test_follow_threads_page_id(self):
        session = stub({"CometPageFollowCommitMutation": FOLLOW_RESPONSE})
        response = PagesService(session).follow("555")
        assert response == FOLLOW_RESPONSE
        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == "CometPageFollowCommitMutation"
        assert doc_id == "29690201327260308"
        assert variables["input"]["subscribee_id"] == "555"
        assert variables["input"]["actor_id"] == ACTOR_ID
        assert variables["input"]["subscribe_location"] == "PAGE_PROFILE_HEADER"

    def test_fresh_client_mutation_id_per_call(self):
        session = stub({"CometPageLikeCommitMutation": LIKE_RESPONSE,
                        "CometPageFollowCommitMutation": FOLLOW_RESPONSE,
                        "AdditionalProfilePlusCreationMutation":
                            PAGE_CREATE_RESPONSE})
        service = PagesService(session)
        seen = set()
        for _ in range(2):
            service.like("555")
            service.follow("555")
            service.create("Again Page")
        for _, _, variables in session.graphql.calls:
            mutation_id = variables["input"]["client_mutation_id"]
            uuid.UUID(mutation_id)  # uuid-shaped
            assert mutation_id not in seen
            seen.add(mutation_id)
        assert len(seen) == 6


# ------------------------------------------------------------- command wiring
class TestCommands:
    """Pins the groups/pages argparse wiring with the session constructor
    stubbed out."""

    def _run(self, monkeypatch, capsys, session, argv) -> tuple[int, str]:
        parser = build_parser()
        args = parser.parse_args(argv)
        monkeypatch.setattr(groups_cmd, "new_session", lambda _a: session)
        monkeypatch.setattr(pages_cmd, "new_session", lambda _a: session)
        code = args.fn(args)
        captured = capsys.readouterr().out
        return code, captured

    def test_groups_create_command(self, monkeypatch, capsys):
        session = stub({"useGroupsCometCreateMutation": CREATE_RESPONSE})
        code, out = self._run(
            monkeypatch, capsys, session,
            ["groups", "create", "--name", "CLI Group",
             "--visibility", "public", "--member-ids", "111,222"])
        assert code == 0
        assert "group created: 123" in out
        assert '"id": "123"' in out
        _, _, variables = session.graphql.calls[0]
        assert variables["input"]["members"] == ["111", "222"]
        assert variables["input"]["privacy"] == "PUBLIC"

    def test_groups_join_command(self, monkeypatch, capsys):
        session = stub({"GroupCometJoinForumMutation": JOIN_RESPONSE})
        code, out = self._run(monkeypatch, capsys, session,
                             ["groups", "join", "--group-id", "990011"])
        assert code == 0
        assert "join requested for group 990011" in out
        _, _, variables = session.graphql.calls[0]
        assert variables["input"]["group_id"] == "990011"

    def test_groups_add_members_command(self, monkeypatch, capsys):
        session = stub({"useGroupAddMembersMutation": ADD_MEMBERS_RESPONSE})
        code, out = self._run(
            monkeypatch, capsys, session,
            ["groups", "add-members", "--group-id", "990011",
             "--member-ids", "111,222"])
        assert code == 0
        assert "invited 2 member(s)" in out
        _, _, variables = session.graphql.calls[0]
        assert variables["input"]["user_ids"] == ["111", "222"]

    def test_groups_request_join_command(self, monkeypatch, capsys):
        session = stub({"GroupsCometRequestToParticipateMutation":
                        REQUEST_RESPONSE})
        code, out = self._run(monkeypatch, capsys, session,
                             ["groups", "request-join",
                              "--group-id", "990011"])
        assert code == 0
        assert "participation requested" in out
        _, doc_id, _ = session.graphql.calls[0]
        assert doc_id == "27259944290349383"

    def test_pages_create_command(self, monkeypatch, capsys):
        session = stub({"AdditionalProfilePlusCreationMutation":
                        PAGE_CREATE_RESPONSE})
        code, out = self._run(monkeypatch, capsys, session,
                             ["pages", "create", "--name", "CLI Page",
                              "--category", "Public Figure"])
        assert code == 0
        assert "page created: 777" in out
        assert '"page": {"id": "777"}' in out
        _, _, variables = session.graphql.calls[0]
        assert variables["input"]["name"] == "CLI Page"
        assert variables["input"]["categories"] == ["Public Figure"]

    def test_pages_like_command_trims_payload(self, monkeypatch, capsys):
        session = stub({"CometPageLikeCommitMutation": LIKE_RESPONSE})
        code, out = self._run(monkeypatch, capsys, session,
                              ["pages", "like", "--page-id", "555"])
        assert code == 0
        assert "page 555 liked: 555" in out
        assert '"is_viewer_fan"' not in out  # huge payloads are trimmed

    def test_pages_follow_command(self, monkeypatch, capsys):
        session = stub({"CometPageFollowCommitMutation": FOLLOW_RESPONSE})
        code, out = self._run(monkeypatch, capsys, session,
                              ["pages", "follow", "--page-id", "555"])
        assert code == 0
        assert "page 555 followed" in out
        _, _, variables = session.graphql.calls[0]
        assert variables["input"]["subscribee_id"] == "555"
