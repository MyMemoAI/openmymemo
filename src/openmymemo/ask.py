"""Asking in the app (product-design.md §22): when an app's change waits for the person and the app can show forms
(MCP elicitation), OpenMyMemo puts the question to them right there and applies or rejects the change from the answer.

This module decides whether and how a call can ask, and words the question and the outcome; ``memory_propose`` in
``mcp_server.py`` asks, and ``Proposals.asked`` / ``Proposals.answered`` record it.
"""

from __future__ import annotations

import json
from typing import Literal

from mcp_types import ElicitRequest, ElicitRequestFormParams, InputRequiredResult
from mcp_types.version import is_version_at_least

from .connectors import Connector
from .proposals import ASKABLE, Proposal

TIMEOUT = 600.0  # seconds to wait for the answer during the call; the SDK's sealed request_state lasts as long
FORM = {"type": "object", "properties": {}}  # no fields: the button the person presses is the answer
KEY = "answer"  # the input request's key in an input_required result
INPUT_REQUIRED_SINCE = "2026-07-28"  # from this protocol version a server can't send requests of its own
REASON_MAX = 300

# What memory_propose tells the AI once the person answered, in place of the usual message.
ANSWERED = {
    "accept": "Applied: the user accepted it. It is in their OpenMyMemo digest, where they can still undo it.",
    "decline": "The user declined this change; don't propose it again.",
    "cancel": "Waiting for the user: they left it for later; they can decide in OpenMyMemo.",
    "changed": "Waiting for the user: the memory changed before their answer could be applied; they can decide in "
               "OpenMyMemo.",
}

How = Literal["input_required", "request"]


def wanted(connector: Connector, result: dict) -> bool:
    """Whether to ask about what ``memory_propose`` just filed: a new proposal that waits because the person wrote the
    memory or reviews every change (never a private or sensitive one), from an app on this computer that hasn't had
    asking turned off. Never about a document (``doc:`` ids, MEMORY_FORMAT.md §3.6): its change is read as a diff of a
    long text, which the panel shows and a dialog can't."""
    return (connector.ask and connector.kind in ("stdio", "http") and result.get("status") == "pending"
            and result.get("hold") in ASKABLE and not result.get("deduplicated")
            and not str(result.get("memory_id") or "").startswith("doc:"))


def how(ctx) -> How | None:
    """How this call can carry the question: an ``input_required`` result (protocol 2026-07-28 and later), an
    ``elicitation/create`` request during the call (earlier versions, on a connection that can carry one), or not at
    all (the app can't show forms, or there is no request in flight)."""
    try:
        capabilities = ctx.client_capabilities
        version = ctx.protocol_version
        can_send = ctx.session.can_send_request
    except Exception:  # no request context, e.g. a direct call
        return None
    elicitation = capabilities.elicitation if capabilities is not None else None
    # A bare `elicitation: {}` (from before modes existed) means forms; URL mode alone doesn't.
    if elicitation is None or (elicitation.form is None and elicitation.url is not None):
        return None
    if version is not None and is_version_at_least(version, INPUT_REQUIRED_SINCE):
        return "input_required"
    return "request" if can_send else None


def question(p: Proposal, titles: dict[str, str]) -> str:
    """What the app shows the person: who wants what where, the text now and after, the app's reason, and what each
    answer does."""
    def title(slug: str | None) -> str:
        return titles.get(slug or "", slug or "")

    where = title(p.topic)
    if p.op == "update" and p.before_topic and p.before_topic != p.topic:
        where = f"{title(p.before_topic)} → {where}"
    yours = " you wrote" if p.hold == "protected" else ""
    if p.op == "add":
        lines = [f"{p.connector_name} wants to remember something ({where}).", "", p.content or ""]
    elif p.op == "archive":
        lines = [f"{p.connector_name} wants to archive a memory{yours} ({where}).", "", p.before_content or ""]
    else:
        lines = [f"{p.connector_name} wants to change a memory{yours} ({where}).", "",
                 f"Now: {p.before_content or ''}", f"New: {p.content or ''}"]
    reason = " ".join((p.reason or "").split())
    if reason:
        cut = reason if len(reason) <= REASON_MAX else reason[:REASON_MAX - 1].rstrip() + "…"
        lines += ["", f"Why: {cut}"]
    lines += ["", "Accept makes the change now; you can still undo it in OpenMyMemo. Decline turns it down. "
                  "Cancel leaves it waiting in OpenMyMemo."]
    return "\n".join(lines)


def input_required(p: Proposal, titles: dict[str, str]) -> InputRequiredResult:
    """The question as an ``input_required`` result: the app asks and calls ``memory_propose`` again with the answer
    and this ``request_state`` (which the SDK seals), naming the proposal."""
    form = ElicitRequestFormParams(message=question(p, titles), requested_schema=FORM)
    return InputRequiredResult(input_requests={KEY: ElicitRequest(params=form)},
                               request_state=json.dumps({"proposal": p.id}))


def proposal_from(state: str) -> str:
    """The proposal id in a ``request_state`` this server issued; ValueError for anything else."""
    pid = json.loads(state).get("proposal")
    if not isinstance(pid, str) or not pid.isalnum():
        raise ValueError("no proposal in request_state")
    return pid


def outcome(result: dict, answer: str) -> dict:
    """``memory_propose``'s result after a question, with a message that says what the person did."""
    if answer in ANSWERED:
        result["message"] = ANSWERED[answer]
    return result
