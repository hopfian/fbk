"""Messenger commands: threads, history, send, listen (docs/06 realtime
protocol; docs/15 live calibration).

Reads (threads/history) ride the GraphQL message-range family
(docs/02 §2.2); `send` fires the live-verified CometSendMessageMutation
with an E2EE guard (docs/15 §P7); `listen` subscribes to the realtime
bus — the MQIsdp MQTT bus by default, or the DGW lightspeed socket
with ``--dgw`` (docs/15 §P3-7). Human output never prints message
bodies verbatim — heads only (see _head).

USER-DOC ANCHOR: cli/docs/06-reference-people.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
from typing import Any

from session import Session
from surfaces.messenger import MessengerService

from .common import add_common_args, emit, new_session, with_session


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the messenger family onto the root parser: threads, history,
    send, listen."""
    messenger = sub.add_parser(
        "messenger", help="Messenger surface: threads, history, send, listen (docs/06)")
    msub = messenger.add_subparsers(dest="messenger_command", required=True)

    threads = msub.add_parser("threads", help="list recent message threads")
    add_common_args(threads)
    threads.add_argument("--limit", type=int, default=20,
                         help="maximum number of threads to return (default 20)")
    threads.set_defaults(fn=cmd_threads)

    history = msub.add_parser(
        "history", help="read one thread's recent messages (EB message range)")
    add_common_args(history)
    history.add_argument("--thread-id", required=True,
                         help="target thread key (fbk messenger threads); raw "
                              "numeric or b64 Thread:<n> global id")
    history.add_argument("--limit", type=int, default=20,
                         help="maximum number of messages to return (default 20)")
    history.set_defaults(fn=cmd_history)

    send = msub.add_parser("send", help="send a text message to one thread")
    add_common_args(send)
    send.add_argument("thread_id", help="target thread key (fbk messenger threads)")
    send.add_argument("--text", required=True, help="message text to send")
    send.add_argument("--force", action="store_true",
                       help="override the E2EE guard: the plaintext wire only "
                            "reaches the self/AI chat; P2P threads silently "
                            "misroute (docs/15 §P7)")
    send.set_defaults(fn=cmd_send)

    listen = msub.add_parser(
        "listen", help="subscribe to the realtime bus and collect frames")
    add_common_args(listen)
    listen.add_argument("--dgw", action="store_true",
                        help="listen on the DGW lightspeed socket instead "
                             "of the MQTT bus (docs/15 §P3-7)")
    listen.add_argument("--seconds", type=float, default=30.0,
                        help="how long to listen before closing (default 30.0s)")
    listen.add_argument("--topics", default=None,
                        help="comma-separated topic list (default /t_ms,/t_rtc_multi)")
    listen.set_defaults(fn=cmd_listen)


def _service(session: Session) -> MessengerService:
    """A MessengerService bound to the given live session.

    The caller owns the session's lifecycle (commands.common.
    with_session) — the service is transport-bound, so the session
    must outlive every service call in the command body.
    """
    return MessengerService(session)


def _head(text: str | None, size: int = 24) -> str:
    """Whitespace-collapsed head of free text, ellipsis-marked.

    Message bodies are PII: they never print verbatim in human output —
    heads suffice to identify a thread/message, and the full text stays
    in the --json payload and journal where the operator's own redaction
    policy applies (docs/11 §7).
    """
    if not text:
        return ""
    cleaned = " ".join(text.split())
    return cleaned[:size] + ("…" if len(cleaned) > size else "")


def _iso_utc(timestamp_ms: int | None) -> str:
    """ISO-8601 UTC rendering of one message timestamp."""
    if not timestamp_ms:
        return "?"
    return datetime.fromtimestamp(
        timestamp_ms / 1000.0, tz=UTC).isoformat(timespec="seconds")


def cmd_history(args: argparse.Namespace) -> int:
    """Read one thread's recent messages (the EB message range read).

    ``--thread-id`` accepts the raw numeric thread key from
    `messenger threads` or the b64 ``Thread:<n>`` global id — the
    service normalizes both. Human lines print sender, text head, and
    a UTC timestamp (full bodies only in --json).

    Returns:
        0 — an empty history is valid account state.
    """
    with with_session(new_session(args)) as session:
        service = _service(session)
        messages = service.history(args.thread_id, limit=args.limit)

        def human() -> None:
            for message in messages:
                sender = (message.sender.name or message.sender.id
                          if message.sender else "?")
                print(f"{sender} | {_head(message.text) or '(no text)'} "
                      f"| {_iso_utc(message.timestamp_ms)}")

        payload = {"thread_id": args.thread_id,
                   "messages": [m.model_dump() for m in messages]}
        emit(args, payload, human=human)
        return 0


def cmd_threads(args: argparse.Namespace) -> int:
    """List recent message threads (thread-list read, docs/02 §2.2).

    Human lines show the thread key (the handle `history`/`send`
    take), sanitized name head, participant count, and snippet head —
    self-threads are marked, they are the E2EE-safe send targets.

    Returns:
        0 — zero threads is valid account state.
    """
    with with_session(new_session(args)) as session:
        service = _service(session)
        threads = service.threads(limit=args.limit)

        def human() -> None:
            for thread in threads:
                suffix = " self-thread" if thread.is_self_thread else ""
                print(f"{thread.id}  {_head(thread.name) or '(unnamed)'}"
                      f"  [{len(thread.participants)} participants]"
                      f"  {_head(thread.snippet)}{suffix}")

        payload = {"threads": [t.model_dump() for t in threads]}
        emit(args, payload, human=human)
        return 0


def cmd_send(args: argparse.Namespace) -> int:
    """Send a text message to one thread (CometSendMessageMutation,
    docs/02 §2.2 send family).

    ``--force`` overrides the E2EE guard: the plaintext wire only
    reaches the self/AI chat — P2P-encrypted threads accept the send
    but silently misroute (docs/15 §P7), so the guard refuses them
    without the explicit operator acknowledgment.

    Returns:
        0 when the response echoes message rows (sent_ok); 1 when it
        does not — an unconfirmed mutation is a failed precondition
        (docs/10 §3.4 soft-success discipline), never counted as sent.
    """
    with with_session(new_session(args)) as session:
        service = _service(session)
        response = service.send(args.thread_id, args.text, force=args.force)
        sent = MessengerService.sent_ok(response)

        def human() -> None:
            status = "OK" if sent else "no message rows echoed"
            print(f"send to {args.thread_id}: {status}")

        emit(args, {"thread_id": args.thread_id, "sent": sent}, human=human)
        return 0 if sent else 1


def cmd_listen(args: argparse.Namespace) -> int:
    """Collect realtime frames for --seconds from the bus, then close.

    ``--dgw`` selects the DGW lightspeed socket (docs/15 §P3-7); the
    default path is the MQIsdp MQTT bus with /t_ms + /t_rtc_multi
    subscriptions (docs/06 §3). MQTT lines print frame kind, topic,
    size, and payload key names only — delta contents stay in the
    --json payload; DGW lines print the decoded payload directly.

    Returns:
        0 — a silent bus (zero frames) is a valid observation, not a
        connection failure.
    """
    with with_session(new_session(args)) as session:
        service = _service(session)
        if args.dgw:
            frames = service.listen_dgw(seconds=args.seconds)
            for index, frame in enumerate(frames, start=1):
                print(f"[{index}] rid={frame['request_id']} "
                      f"{frame['payload_type']} {frame['payload']}")
            emit(args, {"frames": frames})
            return 0
        topics: list[str] | None = None
        if args.topics:
            topics = [t.strip() for t in args.topics.split(",") if t.strip()]
        frames = service.listen(topics, seconds=args.seconds)
        for index, frame in enumerate(frames, start=1):
            summary: Any = frame["summary"]
            keys = ",".join(summary.keys()) if isinstance(summary, dict) else ""
            print(f"[{index}] {frame['kind']} {frame['topic']} "
                  f"{frame['size']}B {keys}")
        emit(args, {"frames": frames})
        return 0
