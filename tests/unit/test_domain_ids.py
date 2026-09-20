"""Domain identifier tests — using live-captured id values (docs/14).

Unit (offline): pure construction/decode checks against b64 ids captured
during Phases 2-3 (docs/15) — no fixtures, no session, no network.
"""
import base64

import pytest

from domain.common import CommentID, FeedbackID, Privacy, ReactionType


class TestFeedbackID:
    """Pins FeedbackID b64 <-> numeric round-trips against the live-captured
    id (P2-2)."""

    def test_from_live_captured_b64(self):
        """P2-2 captured 'ZmVlZGJhY2s6MzkzNzAyNDMyOTc3NDAzNg=='."""
        fid = FeedbackID.from_b64("ZmVlZGJhY2s6MzkzNzAyNDMyOTc3NDAzNg==")
        assert fid.numeric == "3937024329774036"

    def test_from_numeric_roundtrip(self):
        fid = FeedbackID.from_numeric("3937024329774036")
        assert base64.b64decode(fid.raw + "==").decode() == "feedback:3937024329774036"
        assert FeedbackID.from_b64(fid.raw) == fid

    def test_accepts_decoded_form(self):
        assert FeedbackID.from_b64("feedback:123").numeric == "123"

    def test_rejects_empty(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            FeedbackID(raw="")


class TestCommentID:
    """Pins CommentID b64 <-> (post, comment) pair round-trips against the
    live capture (P3-3)."""

    def test_from_live_captured_b64(self):
        """P3-3 captured 'Y29tbWVudDozOTM3MzQxNTY2NDA4OTc5XzM5MzgyNDc4ODYzMTgzNDc='."""
        cid = CommentID.from_b64(
            "Y29tbWVudDozOTM3MzQxNTY2NDA4OTc5XzM5MzgyNDc4ODYzMTgzNDc=")
        assert cid.decoded == ("3937341566408979", "3938247886318347")

    def test_from_numeric_roundtrip(self):
        cid = CommentID.from_numeric("3937341566408979", "3938247886318347")
        inner = base64.b64decode(cid.raw + "==").decode()
        assert inner == "comment:3937341566408979_3938247886318347"
        assert CommentID.from_b64(cid.raw) == cid


class TestReactionType:
    """Pins the seven-reaction enum decoded from CometUFIFunnelLogger
    (P3-2)."""

    def test_values_are_reaction_ids(self):
        assert ReactionType.LIKE.reaction_id == "1635855486666999"
        assert ReactionType.SUPPORT.reaction_id == "613557422527858"

    def test_from_name(self):
        assert ReactionType.from_name("like") is ReactionType.LIKE
        assert ReactionType.from_name("ANGER") is ReactionType.ANGER

    def test_full_enum_matches_live_capture(self):
        """The seven-reaction enum decoded from CometUFIFunnelLogger (P3-2)."""
        assert {r.name for r in ReactionType} == {
            "LIKE", "LOVE", "WOW", "HAHA", "SORRY", "ANGER", "SUPPORT"}


class TestPrivacy:
    """Pins the privacy base_state wire spellings from the P3 composer
    ground truth."""

    def test_base_states_match_live_capture(self):
        """P3 ground truth: audience.privacy.base_state — 'FRIENDS' captured."""
        assert Privacy.FRIENDS.value == "FRIENDS"
        assert Privacy.PUBLIC.value == "EVERYONE"
        assert Privacy.PRIVATE.value == "SELF"

    def test_from_name(self):
        assert Privacy.from_name("public") is Privacy.PUBLIC
        assert Privacy.from_name("private") is Privacy.PRIVATE
