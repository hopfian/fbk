"""domain.common validation edges (wave 2 polish targets).

test_domain_ids.py pins the live-captured identifier semantics; what was
missing is the validation envelope:

  * invalid enum VALUES are rejected (ValueError from the enum itself);
  * unknown from_name() lookups raise the TYPED UnknownNameError with the
    valid names listed in the message;
  * missing required fields raise pydantic ValidationError;
  * model-typed fields (Feedback.id) demand real instances — no silent
    string coercion;
  * garbage ids degrade gracefully on decode, never raise.

Unit (offline): pure model construction — no fixtures, no session, no
network.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.common import (
    Actor,
    CommentID,
    Cursor,
    Feedback,
    FeedbackID,
    NotificationCount,
    Privacy,
    Profile,
    ReactionType,
    SearchResult,
    Story,
    StoryKey,
    UnknownNameError,
    User,
)


class TestEnumValueValidation:
    """Pins enum value strictness and that ReactionType members ARE their
    wire reaction ids (mutation-ready)."""

    def test_invalid_reaction_value_rejected(self):
        with pytest.raises(ValueError, match="not a valid ReactionType"):
            ReactionType("BANANA")

    def test_invalid_privacy_value_rejected(self):
        with pytest.raises(ValueError, match="not a valid Privacy"):
            Privacy("BANANA")

    def test_reaction_values_are_the_wire_reaction_ids(self):
        # StrEnum: the member IS its id string (mutation-ready)
        assert ReactionType.LIKE == "1635855486666999"
        assert ReactionType.LIKE.reaction_id == "1635855486666999"


class TestFromNameLookup:
    """Pins from_name(): typed UnknownNameError listing the valid names,
    case-insensitive and trimmed input."""

    def test_unknown_reaction_name_is_a_typed_error(self):
        with pytest.raises(UnknownNameError) as ei:
            ReactionType.from_name("BANANA")
        assert "BANANA" in str(ei.value)
        for name in ("LIKE", "LOVE", "WOW", "HAHA", "SORRY", "ANGER", "SUPPORT"):
            assert name in str(ei.value)

    def test_unknown_privacy_name_is_a_typed_error(self):
        with pytest.raises(UnknownNameError, match="PUBLIC"):
            Privacy.from_name("nope")

    def test_unknown_name_error_keeps_both_historical_contracts(self):
        """Callers may catch either KeyError or ValueError — both still work."""
        assert issubclass(UnknownNameError, KeyError)
        assert issubclass(UnknownNameError, ValueError)

    def test_from_name_is_case_insensitive_and_trimmed(self):
        assert ReactionType.from_name(" like ") is ReactionType.LIKE
        assert Privacy.from_name("Private") is Privacy.PRIVATE


class TestMissingRequiredFields:
    """Pins that every required-field model refuses empty construction
    with pydantic ValidationError."""

    @pytest.mark.parametrize("factory", [
        lambda: FeedbackID(),
        lambda: CommentID(),
        lambda: StoryKey(),
        lambda: Cursor(),
        lambda: Feedback(),
        lambda: SearchResult(),
        lambda: NotificationCount(),
        lambda: Profile(),
        lambda: User(),
    ], ids=["FeedbackID", "CommentID", "StoryKey", "Cursor", "Feedback",
            "SearchResult", "NotificationCount", "Profile", "User"])
    def test_missing_required_field_is_a_validation_error(self, factory):
        with pytest.raises(ValidationError):
            factory()

    def test_story_is_all_optional(self):
        assert Story().feedback is None
        assert Story().actor is None


class TestModelStrictness:
    """Pins model-typed fields: no silent string coercion into FeedbackID,
    empty ids rejected, alias-driven typename population."""

    def test_feedback_requires_a_real_feedbackid(self):
        """No silent coercion: a bare string is not a FeedbackID."""
        with pytest.raises(ValidationError):
            Feedback(id="ZmVlZGJhY2s6MTIz")

    def test_feedback_with_a_feedbackid_instance_is_accepted(self):
        fb = Feedback(id=FeedbackID.from_numeric("123"))
        assert fb.id.numeric == "123"

    def test_actor_populates_through_the_typename_alias(self):
        actor = Actor(id="1", **{"__typename": "User"})
        assert actor.typename == "User"

    def test_empty_feedback_id_is_rejected(self):
        with pytest.raises(ValidationError, match="empty feedback id"):
            FeedbackID(raw="")


class TestGarbageIdsDegradeGracefully:
    """Pins decode-time garbage tolerance: malformed ids degrade to
    empty/raw values, never raise."""

    def test_commentid_garbage_decodes_to_an_empty_pair(self):
        assert CommentID(raw="!!!").decoded == ("", "")

    def test_feedbackid_garbage_numeric_falls_back_to_raw(self):
        assert FeedbackID(raw="not-base64!").numeric == "not-base64!"

    def test_feedbackid_numeric_on_a_plain_number(self):
        # decodes to "123" with no colon: split keeps the whole payload
        assert FeedbackID(raw=FeedbackID.from_numeric("1").raw).numeric == "1"
