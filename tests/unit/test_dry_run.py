"""--dry-run: the plan-and-abort request mode.

Pins the full contract of the offline request-plan mode:

  * plan content — friendly_name, doc_id, endpoint, the friendly-name
    suffix mutation classification, and the would-debit estimate —
    driven through GraphQLClient(dry_run=True) on the same StubTransport
    fakes as test_graphql_client.py, plus one FULL-pipeline run
    (template load -> variable substitution -> doc_id resolution ->
    classification) through the real FeedService on a StubSession whose
    .graphql is swapped for a dry-run client;
  * the redaction pin: secret-named variable fields never print
    (journal.recorder.redact_entry / ALL_SECRETS, docs/11 §7);
  * zero side effects — the exploding-governor pattern (the raw-seam
    precedent in test_transport_raw_seam.py) plus a real FBTransport:
    no send, no governor tick, no journal entry, no q consumption
    (docs/11 §8: budgets count wire volume, and a plan reaches no edge);
  * the exit contract — DryRunComplete exits 0 through run_command,
    DryRunRawSeamError exits 1, neither is retried under --retry;
  * Session wiring — the flag forwards to the client and installs the
    raw-seam refusal guard; a default Session stays byte-identical
    (no guard, flag False) so the mode is inert by default;
  * add_common_args wiring — --dry-run parses on a real command family.

Unit (offline): no network anywhere; the StubTransport queues are
EMPTY because nothing may ever be sent.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fakes import StubSession, StubTransport
from pydantic import SecretStr

import constants as C
import session as session_module
from auth.bootstrap import Bootstrap
from auth.state import LoginState
from commands.common import _EXIT_CODES, run_command
from config import Config
from domain.common import ReactionType
from graphql.client import GraphQLClient
from graphql.errors import (
    DryRunComplete,
    DryRunRawSeamError,
    FBGraphError,
)
from graphql.registry import DocIdRegistry
from journal.recorder import JSONLJournal
from session import Session
from surfaces.feed import FeedService
from transport.profile import ClientProfile
from transport.session import FBTransport

CLI_ROOT = Path(__file__).resolve().parents[2]

REACT_MUTATION = "CometUFIFeedbackReactMutation"
BADGE_QUERY = "CometNotificationsBadgeCountQuery"  # the KNOWN_MUTATIONS read


def make_bootstrap() -> Bootstrap:
    """The same logged-in bootstrap shape test_graphql_client.py uses."""
    return Bootstrap(state=LoginState.LOGGED_IN,
                     fb_dtsg=SecretStr("NAfTEST:1:1789723298"),
                     lsd=SecretStr("TESTLSD"), user_id="1")


def dry_client(transport: Any, *,
               registry: DocIdRegistry | None = None) -> GraphQLClient:
    """A GraphQLClient in plan-and-abort mode against the given transport."""
    return GraphQLClient(transport, make_bootstrap(), registry=registry,
                         dry_run=True)


# ------------------------------------------------------------------ plan content
class TestPlanContent:
    """Pins the printed plan: identity fields, suffix classification,
    and the debit estimate — against stubs, nothing queued to send."""

    def test_mutation_plan_on_a_known_mutations_entry(self, capsys):
        transport = StubTransport([])
        client = dry_client(transport)
        with pytest.raises(DryRunComplete) as exc:
            client.call(REACT_MUTATION,
                        C.KNOWN_MUTATIONS[REACT_MUTATION], {"input": {}})
        plan = exc.value.plan
        assert plan["friendly_name"] == REACT_MUTATION
        assert plan["doc_id"] == C.KNOWN_MUTATIONS[REACT_MUTATION]
        assert plan["endpoint"] == C.GRAPHQL_ENDPOINT
        assert plan["is_mutation"] is True
        assert plan["would_debit"] == "1 mutation"
        out = capsys.readouterr().out
        assert REACT_MUTATION in out
        assert C.KNOWN_MUTATIONS[REACT_MUTATION] in out
        assert "classification: mutation (would debit: 1 mutation)" in out
        assert "--- dry-run request plan ---" in out
        assert transport.posts == []  # nothing ever reached the transport

    def test_read_plan_classifies_and_debits_the_read_budget(self, capsys):
        client = dry_client(StubTransport([]))
        with pytest.raises(DryRunComplete) as exc:
            client.call(BADGE_QUERY, C.KNOWN_MUTATIONS[BADGE_QUERY], {})
        assert exc.value.plan["is_mutation"] is False
        assert exc.value.plan["would_debit"] == "1 read"
        out = capsys.readouterr().out
        assert "classification: read (would debit: 1 read)" in out

    def test_call_by_name_resolves_doc_id_before_the_stop(self, capsys):
        """Registry dispatch is part of the plan: the doc_id in the
        printed plan is the RESOLVED one, and the miss path still
        raises RegistryMissError under dry-run (resolution is real)."""
        reg = DocIdRegistry.from_pairs({"CometKnown": "42"})
        client = dry_client(StubTransport([]), registry=reg)
        with pytest.raises(DryRunComplete) as exc:
            client.call_by_name("CometKnown", {"x": 1})
        assert exc.value.plan["doc_id"] == "42"

    def test_call_raw_stops_identically(self, capsys):
        """The primitive both call() and call_by_name() ride stops too."""
        with pytest.raises(DryRunComplete):
            dry_client(StubTransport([])).call_raw(REACT_MUTATION, "7", {})
        assert "doc_id: 7" in capsys.readouterr().out


class TestFullPipeline:
    """Drives the REAL surface stack — template load, variable
    substitution, doc_id resolution — against a dry-run client."""

    def _service(self, session: StubSession) -> FeedService:
        """A StubSession whose .graphql is a real dry-run client."""
        session.graphql = dry_client(StubTransport([]),
                                     registry=session.registry)
        return FeedService(session)  # type: ignore[arg-type]

    def test_feed_react_plans_the_substituted_mutation(self, capsys):
        """Template load -> feedback_id substitution -> KNOWN_MUTATIONS
        doc_id -> mutation classification, end to end, offline."""
        service = self._service(StubSession())
        with pytest.raises(DryRunComplete) as exc:
            service.react("ZmVlZGJhY2s6MTIzNDU2Nzg5", ReactionType.LIKE)
        plan = exc.value.plan
        assert plan["friendly_name"] == REACT_MUTATION
        assert plan["doc_id"] == C.KNOWN_MUTATIONS[REACT_MUTATION]
        assert plan["is_mutation"] is True
        out = capsys.readouterr().out
        # the substituted feedback id rides in the plan's variables
        assert "ZmVlZGJhY2s6MTIzNDU2Nzg5" in out
        assert "1635855486666999" in out  # the LIKE reaction id (docs/15 P2-3)


# -------------------------------------------------------------------- redaction
class TestRedactionPin:
    """Secret-named variable values never print (docs/11 §7 discipline)."""

    def test_secret_named_variable_fields_print_as_fingerprints(self, capsys):
        variables = {
            "feedback_id": "ZmVlZGJhY2s6MTIzNDU2Nzg5",   # protocol data: prints
            "xs": "SECRET-XS-VALUE",
            "fb_dtsg": "SECRET-DTSG-VALUE",
            "access_token": "SECRET-ACCESS-TOKEN",
        }
        with pytest.raises(DryRunComplete):
            dry_client(StubTransport([])).call(BADGE_QUERY, "9", variables)
        out = capsys.readouterr().out
        assert "SECRET-XS-VALUE" not in out
        assert "SECRET-DTSG-VALUE" not in out
        assert "SECRET-ACCESS-TOKEN" not in out
        # the redaction form is the journal fingerprint (docs/11 §7)
        assert out.count("<redacted:") == 3
        # non-secret protocol data prints in full, like `templates show`
        assert "ZmVlZGJhY2s6MTIzNDU2Nzg5" in out

    def test_the_plan_attribute_is_pre_redacted(self):
        """The plan carried on the sentinel is the SAME redacted copy
        that was printed — no caller can reach the raw variables."""
        client = dry_client(StubTransport([]))
        with pytest.raises(DryRunComplete) as exc:
            client.call(BADGE_QUERY, "9", {"sessionid": "SECRET-IG"})
        rendered = str(exc.value.plan["variables"])
        assert "SECRET-IG" not in rendered
        assert "<redacted:" in rendered


# ---------------------------------------------------------------- zero side effects
class ExplodingGovernor:
    """A governor whose every hook EXPLODES: proves a dry-run never
    ticks pacing, budgets, or soft-block observation."""

    def before_request(self, *, is_mutation: bool = False, quiet: bool = False):
        raise AssertionError("dry-run must not tick the governor")

    def observe_soft_block(self):
        raise AssertionError("dry-run must not observe soft blocks")

    def observe_checkpoint(self):
        raise AssertionError("dry-run must not observe checkpoints")


class NoHTTPSession:
    """Replaces FBTransport._session: any wire call is a test failure."""

    def get(self, url, **kw):
        raise AssertionError("dry-run must not open the wire")

    def post(self, url, **kw):
        raise AssertionError("dry-run must not open the wire")

    def close(self):
        pass


class ExplodingStubTransport(StubTransport):
    """A StubTransport whose send path and q counter both explode."""

    def next_q(self):
        raise AssertionError("dry-run must not consume the q counter")

    def post_graphql(self, url, *, data, friendly_name, lsd, **kw):
        raise AssertionError("dry-run must not reach the transport")


class TestZeroSideEffects:
    """No transport call, no governor tick, no journal entry, no q."""

    def test_stub_level_no_send_no_q(self):
        """The plan stops before next_q() and before post_graphql()."""
        transport = ExplodingStubTransport([])
        with pytest.raises(DryRunComplete):
            dry_client(transport).call(REACT_MUTATION, "27646120298312844", {})

    def test_real_transport_sends_journals_and_ticks_nothing(
            self, tmp_path, capsys):
        """A real FBTransport + exploding governor + live journal: the
        dry-run sends nothing, writes no journal line, advances no q."""
        journal = JSONLJournal(tmp_path / "dry.jsonl")
        transport = FBTransport(
            {"c_user": "1", "xs": "x"}, profile=ClientProfile(),
            journal=journal, timeout=5, impersonate="chrome136",
            governor=ExplodingGovernor())
        transport._session = NoHTTPSession()
        client = GraphQLClient(transport, make_bootstrap(), dry_run=True)
        with pytest.raises(DryRunComplete):
            client.call(REACT_MUTATION, "27646120298312844", {"input": {}})
        out = capsys.readouterr().out
        assert transport._q == 0               # the q counter never advanced
        assert not (tmp_path / "dry.jsonl").exists()  # no journal record
        assert "would debit: 1 mutation" in out


# ----------------------------------------------------------------- exit contract
def _ns(**kw) -> argparse.Namespace:
    """A minimal command namespace (retry default mirrors the parsers)."""
    base = {"retry": 0}
    base.update(kw)
    return argparse.Namespace(**base)


class TestExitContract:
    """run_command maps the dry-run sentinels onto the exit contract."""

    def test_dry_run_complete_exits_0_with_the_summary_line(self, capsys):
        def fn(args):
            raise DryRunComplete({"friendly_name": REACT_MUTATION})
        assert run_command(fn, _ns()) == 0
        assert "dry-run complete — 0 requests sent" in capsys.readouterr().out

    def test_dry_run_complete_is_not_retried(self):
        """A success sentinel must burn exactly one --retry budget slot."""
        calls = []

        def fn(args):
            calls.append(1)
            raise DryRunComplete({"friendly_name": "Q"})
        assert run_command(fn, _ns(retry=3)) == 0
        assert len(calls) == 1

    def test_dry_run_sentinels_sit_outside_the_graph_error_family(self):
        """The design decision: surface degradation handlers catch
        FBGraphError broadly (surfaces/measurement.py would log a plan
        as a failed sample) — a success sentinel must never be
        swallowed, and the raw-seam refusal must never be degraded."""
        assert not issubclass(DryRunComplete, FBGraphError)
        assert not issubclass(DryRunRawSeamError, FBGraphError)
        # and neither is in the typed-error exit-code table
        assert not any(issubclass(exc_type, (DryRunComplete, DryRunRawSeamError))
                       for exc_type in _EXIT_CODES)

    def test_raw_seam_refusal_exits_1_on_stderr(self, capsys):
        def fn(args):
            raise DryRunRawSeamError(
                "dry-run covers GraphQL calls only — this command uses the "
                "raw HTTP seam, which has no request plan")
        assert run_command(fn, _ns()) == 1
        captured = capsys.readouterr()
        assert "dry-run covers GraphQL calls only" in captured.err
        assert captured.out == ""  # a refusal prints no payload on stdout


# ---------------------------------------------------------------- session wiring
COOKIES_TXT = (
    "# Netscape HTTP Cookie File\n"
    ".facebook.com\tTRUE\t/\tTRUE\t2147483647\tc_user\t12345678901234\n"
    ".facebook.com\tTRUE\t/\tTRUE\t2147483647\txs\tTEST-XS\n"
    ".facebook.com\tTRUE\t/\tTRUE\t2148483647\tdatr\tTEST-DATR\n"
)


def make_session(tmp_path: Path, monkeypatch, *, dry_run: bool) -> Session:
    """Offline Session on a tmp jar/state dir (the lifecycle pattern),
    with the homepage bootstrap stubbed to never touch the wire."""
    cookies = tmp_path / "cookies.txt"
    cookies.write_text(COOKIES_TXT, encoding="utf-8")
    monkeypatch.setattr(
        session_module, "bootstrap_homepage",
        lambda transport, jar: make_bootstrap())
    cfg = Config.discover(CLI_ROOT).model_copy(update={
        "cookies_path": cookies,
        "state_dir": tmp_path / "state",
    })
    return Session(cfg, journal_name=None, dry_run=dry_run)


class TestSessionWiring:
    """Session forwards the mode to the client and guards the raw seam."""

    def test_dry_run_session_builds_a_dry_run_client(self, tmp_path,
                                                     monkeypatch):
        session = make_session(tmp_path, monkeypatch, dry_run=True)
        client = session.graphql
        assert isinstance(client, GraphQLClient)
        assert client.dry_run is True

    def test_dry_run_session_refuses_the_raw_seam(self, tmp_path,
                                                  monkeypatch):
        session = make_session(tmp_path, monkeypatch, dry_run=True)
        with pytest.raises(DryRunRawSeamError,
                           match="dry-run covers GraphQL calls only"):
            session.transport.raw_post(
                "https://upload.facebook.com/ajax/react_composer/"
                "attachments/photo/upload",
                data=b"bytes", headers={})
        with pytest.raises(DryRunRawSeamError,
                           match="dry-run covers GraphQL calls only"):
            session.transport.raw_get("https://rupload.facebook.com/",
                                       headers={})

    def test_default_session_is_inert(self, tmp_path, monkeypatch):
        """The 1002-test pin: without the flag, nothing changes — the
        transport keeps its real raw seam and the client its live mode."""
        session = make_session(tmp_path, monkeypatch, dry_run=False)
        assert session.dry_run is False
        assert session.graphql.dry_run is False
        # the raw seam is NOT shadowed: no instance-attribute guard
        assert "raw_post" not in vars(session.transport)
        assert "raw_get" not in vars(session.transport)


# ------------------------------------------------------------- CLI flag wiring
class TestCommonArgsWiring:
    """--dry-run parses on an existing command family via add_common_args."""

    def test_parses_on_the_real_app_parser(self):
        from app import build_parser
        args = build_parser().parse_args(["feed", "read", "--dry-run"])
        assert args.dry_run is True

    def test_absent_flag_defaults_false(self):
        from app import build_parser
        args = build_parser().parse_args(["feed", "read"])
        assert args.dry_run is False

    def test_new_session_forwards_the_flag(self, tmp_path, monkeypatch):
        """new_session is the argv -> Session seam: a parsed --dry-run
        namespace produces a dry-run Session."""
        from commands.common import new_session
        (tmp_path / "cookies.txt").write_text(COOKIES_TXT, encoding="utf-8")
        real_discover = Config.discover  # keep the original before patching
        monkeypatch.setattr(
            "config.Config.discover",
            lambda *a, **k: real_discover(CLI_ROOT).model_copy(update={
                "cookies_path": tmp_path / "cookies.txt",
                "state_dir": tmp_path / "state"}))
        args = argparse.Namespace(command="dry-test", no_journal=True,
                                  root=None, cookies=None, dry_run=True)
        session = new_session(args)
        assert isinstance(session, Session)
        assert session.dry_run is True
        session.close()
