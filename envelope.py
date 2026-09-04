"""Execution middleware envelopes for hermes-audit.

Wraps tool and LLM execution so every call is journaled (write-ahead `begin`,
then `complete` with SUCCESS or FAILED) without ever breaking the agent:
if no journal is set or the journal is unhealthy, the real call runs bare.

Middleware contract (Hermes): the callback receives `tool_name`, `args`,
`next_call`, plus arbitrary context kwargs. `next_call(args)` is single-use —
call it EXACTLY ONCE. Whatever this callback returns becomes the tool result;
if it raises, the tool errors.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from schema import (
    TOOL_CALL,
    SKILL_WRITE,
    LLM_CALL,
    SUCCESS,
    FAILED,
)
from journal import AuditJournal
from hooks import get_current_trace_id
from summarize import summarize_event, summarize_tool_call

__all__ = ["set_journal", "get_journal", "tool_execution_envelope", "llm_execution_envelope"]

# Injected at registration time via set_journal().
_journal: Optional[AuditJournal] = None

_SUMMARY_LIMIT = 500


def set_journal(j: Optional[AuditJournal]) -> None:
    """Install the journal instance used by all envelopes (registration hook)."""
    global _journal
    _journal = j


def get_journal() -> Optional[AuditJournal]:
    """Return the currently registered journal, if any."""
    return _journal


def _ready() -> bool:
    """True when journaling is possible; False means run the real call bare."""
    try:
        return _journal is not None and _journal.is_healthy()
    except Exception:
        return False


def summarize(value: Any, limit: int = _SUMMARY_LIMIT) -> str:
    """repr() truncated to `limit` chars (never raises)."""
    try:
        text = repr(value)
    except Exception as exc:  # repr() can raise on exotic objects
        text = "<unrepr-able %s: %s>" % (type(value).__name__, str(exc)[:100])
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


def _duration_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _as_int(value: Any) -> Optional[int]:
    """Coerce a token count to int; None if not cleanly convertible."""
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            return int(value.strip())
    except Exception:
        return None
    return None


def _extract_usage_tokens(usage: Any) -> Optional[dict]:
    """Best-effort structured token extraction from an LLM usage blob.

    Accepts either an attribute-shaped object (e.g. OpenAI CompletionUsage:
    usage.prompt_tokens / usage.completion_tokens / usage.total_tokens) or a
    dict-shaped usage ({'prompt_tokens': ..., ...}). Returns
    {'prompt': int, 'completion': int, 'total': int[, 'cost_usd': float]} or
    None when nothing usable is found. Never raises.
    """
    if usage is None:
        return None

    def _get(name: str) -> Any:
        # Attribute access first (response.usage.prompt_tokens), then dict.
        try:
            v = getattr(usage, name, None)
        except Exception:
            v = None
        if v is None and isinstance(usage, dict):
            try:
                v = usage.get(name)
            except Exception:
                v = None
        return v

    try:
        prompt = _as_int(_get("prompt_tokens"))
        completion = _as_int(_get("completion_tokens"))
        total = _as_int(_get("total_tokens"))
        if prompt is None and completion is None and total is None:
            return None
        out: dict = {"prompt": prompt, "completion": completion, "total": total}
        cost = _get("cost_usd")
        if cost is not None:
            try:
                out["cost_usd"] = float(cost)
            except Exception:
                pass
        return out
    except Exception:
        return None


def tool_execution_envelope(*, tool_name: str, args: dict, next_call: Callable, **context) -> Any:
    """Journal a tool execution, then run the real tool exactly once.

    Never breaks the agent: without a healthy journal the tool runs bare;
    on journaling failure mid-flight the real result/exception still wins.
    """
    if not _ready():
        return next_call(args)

    event = None
    try:
        action_type = SKILL_WRITE if tool_name == "skill_manage" else TOOL_CALL
        actor = str(context.get("actor", "assistant"))
        ctx = dict(context)
        # P1 correlation: Hermes passes turn_id in the middleware context
        # (agent_runtime_helpers.py → run_tool_execution_middleware, set at turn
        # start in turn_context.py:684 as session:task:uuid8). Map it to trace_id
        # so every tool call joins its turn's request-group. Falls back to the
        # message-published trace only if turn_id is somehow absent.
        if not ctx.get("trace_id"):
            ctx["trace_id"] = ctx.get("turn_id") or get_current_trace_id() or None
        # P2: deterministic human summary generated at log time from tool+args.
        # Best-effort: a summarizer failure must never block journaling.
        try:
            ctx["human_summary"] = summarize_tool_call(tool_name, args)
        except Exception:
            ctx["human_summary"] = None
        event = _journal.begin(
            actor=actor,
            action_type=action_type,
            tool_name=tool_name,
            context=ctx or None,
        )
    except Exception:
        event = None  # journaling must not block execution

    start = time.monotonic()
    try:
        result = next_call(args)  # EXACTLY ONCE
    except Exception as exc:
        if event is not None:
            try:
                _journal.complete(
                    event,
                    FAILED,
                    error=str(exc),
                    duration_ms=_duration_ms(start),
                    detail={"args_summary": summarize(args)},
                )
            except Exception:
                pass
        raise
    # success
    if event is not None:
        try:
            _journal.complete(
                event,
                SUCCESS,
                duration_ms=_duration_ms(start),
                detail={
                    "args_summary": summarize(args),
                    "result_summary": summarize(result),
                },
            )
        except Exception:
            pass
    return result


def llm_execution_envelope(*, request: dict, next_call: Callable, **context) -> Any:
    """Journal an LLM call (model + token/cost usage), then run it exactly once."""
    if not _ready():
        return next_call(request)

    model = None
    try:
        model = request.get("model") if isinstance(request, dict) else None
    except Exception:
        model = None

    event = None
    try:
        actor = str(context.get("actor", "assistant"))
        ctx = dict(context)
        # LLM middleware context also carries turn_id (conversation_loop.py:3442).
        if not ctx.get("trace_id"):
            ctx["trace_id"] = ctx.get("turn_id") or get_current_trace_id() or None
        try:
            ctx["human_summary"] = summarize_event(LLM_CALL, model if isinstance(model, str) else None, {"model": model})
        except Exception:
            ctx["human_summary"] = None
        event = _journal.begin(
            actor=actor,
            action_type=LLM_CALL,
            tool_name=model if isinstance(model, str) else None,
            context=ctx or None,
        )
    except Exception:
        event = None

    start = time.monotonic()
    try:
        response = next_call(request)  # EXACTLY ONCE
    except Exception as exc:
        if event is not None:
            try:
                _journal.complete(
                    event,
                    FAILED,
                    error=str(exc),
                    duration_ms=_duration_ms(start),
                    detail={"model": model},
                )
            except Exception:
                pass
        raise
    # success
    if event is not None:
        try:
            detail: dict = {"model": model}
            # Usage: capture whatever Hermes provides, as-is. Traceability
            # cares that the call happened + which model; token accounting is
            # secondary and stored raw (object -> repr, dict -> as-is).
            try:
                usage = getattr(response, "usage", None)
                if usage is None and isinstance(response, dict):
                    usage = response.get("usage")
                if usage is not None:
                    detail["usage"] = summarize(usage)
                    # P3.5: structured token capture at log time so the UI can
                    # aggregate prompt/completion/total (and cost) per model
                    # without re-parsing the raw repr. Best-effort, never raises.
                    try:
                        tokens = _extract_usage_tokens(usage)
                        if tokens is not None:
                            detail["usage_tokens"] = tokens
                    except Exception:
                        pass
            except Exception:
                pass  # usage is best-effort; never block journaling
            _journal.complete(
                event,
                SUCCESS,
                duration_ms=_duration_ms(start),
                detail=detail,
            )
        except Exception:
            pass
    return response
