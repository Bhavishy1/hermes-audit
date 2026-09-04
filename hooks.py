"""Lifecycle observer hooks for hermes-audit.

Observer callbacks wired into Hermes lifecycle events. Each hook journals a
single audit event via the shared module-level journal (injected here with its
own `set_journal`, same pattern as envelope.py).

Safety contract: a hook NEVER breaks the agent. Without a healthy journal it
returns None silently, and every journaling call is wrapped in try/except.
"""

from __future__ import annotations

from typing import Any, Optional

from schema import (
    TOOL_CALL,
    SESSION_START,
    SESSION_END,
    APPROVAL_REQUEST,
    APPROVAL_GRANTED,
    MESSAGE,
    SUCCESS,
)
from journal import AuditJournal

__all__ = [
    "set_journal",
    "get_journal",
    "on_session_start",
    "on_session_end",
    "on_subagent_start",
    "on_subagent_stop",
    "pre_approval_request",
    "post_approval_response",
    "message_capture",
    "on_pre_tool_call",
]

# Truncation cap for captured assistant message text (keep audit rows small).
_MESSAGE_TEXT_MAX_CHARS = 500

# Injected at registration time via set_journal().
_journal: Optional[AuditJournal] = None

# P1 correlation: the most recent turn_id captured by message_capture, so the
# envelope can adopt it when tool middleware context lacks correlation fields.
_current_trace_id: Optional[str] = None


def set_journal(j: Optional[AuditJournal]) -> None:
    """Install the journal instance used by all hooks (registration hook)."""
    global _journal
    _journal = j


def get_journal() -> Optional[AuditJournal]:
    """Return the currently registered journal, if any."""
    return _journal


def get_current_trace_id() -> Optional[str]:
    """Return the latest published trace ID (turn/session) from message_capture."""
    return _current_trace_id


def _safe_journal(fn) -> None:
    """Run a journaling closure; swallow any failure. Hooks never raise."""
    try:
        fn()
    except Exception:
        pass


def _record(actor: str, action_type: str, detail: Optional[dict] = None,
            tool_name: Optional[str] = None, **hook_kw: Any) -> None:
    """Journal one event if (and only if) the journal is set and healthy.

    ``hook_kw`` carries the hook's original kwargs; we lift correlation IDs
    (session_id / turn_id) so lifecycle events join their session/turn group
    instead of landing with NULL trace. Lifecycle events have no turn_id of
    their own — they adopt the session_id, and the current turn when known.

    Callers pass hook kwargs via ``**kw``; to avoid TypeError when a hook kwarg
    is named actor/action_type/detail/tool_name, callers should route through
    the ``_ckw`` helper which strips reserved keys.
    """
    try:
        journal = _journal
        if journal is None or not journal.is_healthy():
            return
        ctx: dict = {}
        try:
            from summarize import summarize_event
            # args may be nested under detail['args'] (hook payloads like
            # on_pre_tool_call) rather than passed separately — surface them so
            # tool summaries use the real arguments, not the generic fallback.
            args_for_summary = None
            if isinstance(detail, dict):
                maybe = detail.get("args")
                if isinstance(maybe, dict):
                    args_for_summary = maybe
            ctx["human_summary"] = summarize_event(action_type, tool_name, detail, args_for_summary)
        except Exception:
            pass  # summary is best-effort
        # Correlation: forward session_id; adopt current turn trace when present.
        sid = hook_kw.get("session_id")
        if isinstance(sid, str) and sid:
            ctx["session_id"] = sid
        turn = hook_kw.get("turn_id")
        if isinstance(turn, str) and turn:
            ctx["trace_id"] = turn
        elif _current_trace_id:
            ctx["trace_id"] = _current_trace_id
        event = journal.begin(actor=actor, action_type=action_type, tool_name=tool_name,
                              context=ctx or None)
        journal.complete(event, SUCCESS, detail=detail)
    except Exception:
        pass  # a hook must never break the agent


def _ckw(kw: dict) -> dict:
    """Strip hook kwargs that would collide with _record's named params, so
    ``_record(..., **_ckw(kw))`` never raises TypeError on duplicate keys."""
    return {k: v for k, v in kw.items()
            if k not in ("actor", "action_type", "detail", "tool_name")}


# ---------------------------------------------------------------- tool calls

# kw keys that signal a block/veto on a pre_tool_call attempt. Hermes does not
# pass the policy decision in the hook kwargs (it is computed FROM the hooks'
# return values after they run), but a decision-shaped kwarg is surfaced here
# when present so the audit row can distinguish attempts from actual vetoes.
_BLOCK_HINT_KEYS = ("blocked", "block_message", "veto", "vetoed", "decision", "deny", "denied")
_DECISION_ALLOW_VALUES = ("allow", "approve", "pass", "none")


def _looks_blocked(kw: dict) -> bool:
    """True when any kwarg indicates the tool call is (or may be) blocked."""
    for key in _BLOCK_HINT_KEYS:
        value = kw.get(key)
        if not value:
            continue
        if key == "decision" and isinstance(value, str) \
                and value.lower() in _DECISION_ALLOW_VALUES:
            continue
        return True
    return False


def on_pre_tool_call(**kw: Any) -> Optional[None]:
    """Journal a tool-call ATTEMPT before execution (audit finding C1).

    Bound to the ``pre_tool_call`` hook, fired by
    ``hermes_cli.plugins._dispatch_pre_tool_call_hooks`` (via
    ``hermes_cli.lifecycle.invoke_hook``) before a tool runs — at both fire
    sites (agent/tool_executor.py and agent/agent_runtime_helpers.py). The
    kwargs Hermes actually passes are:
      tool_name, args (dict), task_id, session_id, tool_call_id, turn_id,
      api_request_id, middleware_trace
    There is no block/veto decision at fire time — the decision is derived
    from the hooks' own return values AFTER they run, so an observer cannot
    see it. Any decision-shaped kwarg present is surfaced in detail anyway.

    FAIL-CLOSED HOST: ``pre_tool_call`` is a policy hook — a callback that
    raises or times out BLOCKS the tool. This observer must therefore NEVER
    raise and ALWAYS return None. Everything is wrapped; even catastrophic
    input (unrepr-able kwargs) degrades to a plain attempt row or no row.

    Args are journaled through the journal's own redaction path
    (AuditJournal._dump_json redacts sensitive keys + truncates long values
    before serialization), matching the tool_execution envelope's args
    handling. detail['phase']='pre' distinguishes this row from executed
    tool_call rows (no phase key / phase 'execute').
    """
    try:
        tool = kw.get("tool_name")
        args = kw.get("args")
        args = args if isinstance(args, dict) else None
        blocked = _looks_blocked(kw)
        detail: dict = {
            "phase": "pre",
            "blocked": blocked,
        }
        if args is not None:
            detail["args"] = args  # redacted by journal._dump_json on complete
        for key in ("block_message", "veto_reason", "reason", "task_id", "tool_call_id"):
            value = kw.get(key)
            if value:
                detail[key] = value if isinstance(value, str) else repr(value)
        _record(
            actor=str(kw.get("actor", "assistant")),
            action_type=TOOL_CALL,
            detail=detail,
            tool_name=tool if isinstance(tool, str) else None,
            **_ckw(kw),
        )
    except Exception:
        pass  # fail-open observer: never veto, never break the agent
    return None


# ---------------------------------------------------------------- lifecycle

def on_session_start(**kw: Any) -> None:
    """Journal a SESSION_START event."""
    _record(
        actor="assistant",
        action_type=SESSION_START,
        detail={k: repr(v) for k, v in kw.items()} or None,
        **_ckw(kw),
    )


def on_session_end(**kw: Any) -> None:
    """Journal a SESSION_END event."""
    _record(
        actor="assistant",
        action_type=SESSION_END,
        detail={k: repr(v) for k, v in kw.items()} or None,
        **_ckw(kw),
    )


# ---------------------------------------------------------------- subagents

def on_subagent_start(**kw: Any) -> None:
    """Journal a subagent spawn as a TOOL_CALL by actor 'subagent:<id>'."""
    subagent_id = kw.get("subagent_id", "?")
    actor = "subagent:" + str(subagent_id)
    detail: dict = {"event": "subagent_spawn", "subagent_id": str(subagent_id)}
    for key in ("agent_name", "task", "description", "parent_session_id", "session_id"):
        if key in kw:
            detail[key] = repr(kw[key])
    _record(actor=actor, action_type=TOOL_CALL, detail=detail, tool_name="subagent_start", **_ckw(kw))


def on_subagent_stop(**kw: Any) -> None:
    """Journal a subagent stop as a TOOL_CALL by actor 'subagent:<id>'."""
    subagent_id = kw.get("subagent_id", "?")
    actor = "subagent:" + str(subagent_id)
    detail: dict = {"event": "subagent_stop", "subagent_id": str(subagent_id)}
    for key in ("agent_name", "exit_status", "result", "session_id"):
        if key in kw:
            detail[key] = repr(kw[key])
    _record(actor=actor, action_type=TOOL_CALL, detail=detail, tool_name="subagent_stop", **_ckw(kw))


# ---------------------------------------------------------------- approvals

def pre_approval_request(**kw: Any) -> None:
    """Journal an APPROVAL_REQUEST, capturing command/description when present."""
    detail: dict = {}
    for key in ("command", "description", "tool_name", "args", "risk"):
        if key in kw:
            detail[key] = repr(kw[key])
    _record(
        actor=str(kw.get("actor", "assistant")),
        action_type=APPROVAL_REQUEST,
        detail=detail or None,
        tool_name=kw.get("tool_name") if isinstance(kw.get("tool_name"), str) else None,
        **_ckw(kw),
    )


def post_approval_response(**kw: Any) -> None:
    """Journal an APPROVAL_GRANTED (with the user's choice) once answered."""
    detail: dict = {}
    for key in ("command", "description", "choice", "decision", "approved", "denied_reason"):
        if key in kw:
            detail[key] = repr(kw[key])
    _record(
        actor=str(kw.get("actor", "assistant")),
        action_type=APPROVAL_GRANTED,
        detail=detail or None,
        tool_name=kw.get("tool_name") if isinstance(kw.get("tool_name"), str) else None,
        **_ckw(kw),
    )


# ---------------------------------------------------------------- messages

def message_capture(**kw: Any) -> None:
    """Journal the assistant's final text reply as a MESSAGE event.

    Bound to the ``post_llm_call`` hook, which Hermes fires exactly once per
    turn in agent/turn_finalizer.py — AFTER the tool-calling loop completes —
    with ``assistant_response`` set to the final user-visible reply. That is
    the message the user actually received, distinct from the per-API-request
    llm_call envelopes captured by the llm_execution middleware.

    Also publishes ``turn_id`` to the module state so the envelope can adopt
    it as a trace_id fallback for tool calls issued after the reply lands.

    Observer-only: the return value is ignored and we deliberately return
    None so the transform machinery never touches the real reply.
    """
    global _current_trace_id
    text = kw.get("assistant_response")
    detail: dict = {}
    if isinstance(text, str) and text.strip():
        stripped = text.strip()
        detail["text"] = (
            stripped[:_MESSAGE_TEXT_MAX_CHARS] + "…[truncated]"
            if len(stripped) > _MESSAGE_TEXT_MAX_CHARS
            else stripped
        )
    else:
        detail["text"] = None
        detail["raw_type"] = type(text).__name__
    for key in ("session_id", "turn_id", "task_id", "model", "platform"):
        if key in kw and kw[key]:
            detail[key] = kw[key] if isinstance(kw[key], str) else repr(kw[key])
    # Publish the latest traceable ID so the envelope can inherit it
    # (tool middleware receives no turn_id; the next tool call after a reply
    # should join the same trace).
    _current_trace_id = detail.get("turn_id") or detail.get("session_id") or _current_trace_id
    _record(
        actor="assistant",
        action_type=MESSAGE,
        detail=detail or None,
        **_ckw(kw),
    )
    # Durability: one-shot mode (`hermes chat -q`) hard-exits (os._exit) right
    # after the turn, killing the journal's async writer before its 0.5s flush
    # interval — tail-of-turn events (this MESSAGE, session_end) were observed
    # to be silently lost in one-shot runs. Force a synchronous flush so the
    # message event is committed before the process can exit. Bounded by the
    # plugin hook timeout; costs <1s once per turn.
    try:
        journal = _journal
        if journal is not None:
            journal.flush()
    except Exception:
        pass  # a hook must never break the agent
