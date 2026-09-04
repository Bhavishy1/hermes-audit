"""Deterministic human-summary generation for hermes-audit.

Maps (action_type, tool_name, args) -> a plain-language sentence, generated at
log time with zero LLM cost and zero hallucination risk. The audit trail's
value is trustworthiness, so the summarizer must never guess: unknown tools
fall back to "Used <tool_name>".

Templates pull the *salient* argument per tool (query for search, command for
terminal, path for file ops). New tools get templates as they appear.
"""

from __future__ import annotations

import os
from typing import Any, Optional

# Field-length caps keep summaries to one readable line.
_STR_CAP = 80


def _short(value: Any, cap: int = _STR_CAP) -> str:
    """Best-effort short string for embedding in a summary. Never raises."""
    try:
        s = str(value).strip().replace("\n", " ")
        return s if len(s) <= cap else s[: cap - 1] + "…"
    except Exception:
        return "?"


def _basename(path: Any) -> str:
    """Extract the filename from a path arg, tolerating ~ and trailing slashes."""
    try:
        p = str(path).strip().rstrip("/")
        return os.path.basename(p) or p
    except Exception:
        return _short(path, 40)


def _first_arg(args: dict, *keys: str) -> Optional[Any]:
    """Return the first present, non-None arg among keys."""
    for k in keys:
        v = args.get(k)
        if v is not None and v != "":
            return v
    return None


def summarize_tool_call(tool_name: Optional[str], args: Optional[dict]) -> str:
    """One-line plain-language summary of a tool call. Deterministic."""
    name = tool_name or "tool"
    a = args if isinstance(args, dict) else {}

    # --- search / fetch -----------------------------------------------------
    if name in ("web_search",):
        q = _first_arg(a, "query")
        return f'Searched the web for "{_short(q)}"' if q else "Searched the web"
    if name in ("web_extract",):
        urls = a.get("urls")
        if isinstance(urls, list) and urls:
            return f"Fetched {len(urls)} page{'s' if len(urls) != 1 else ''}"
        return "Fetched a page"
    if name in ("web_search_news",):
        q = _first_arg(a, "query")
        return f'Searched news for "{_short(q)}"' if q else "Searched the news"

    # --- files --------------------------------------------------------------
    if name in ("read_file",):
        p = _first_arg(a, "path", "file")
        return f"Read {_basename(p)}" if p else "Read a file"
    if name in ("write_file",):
        p = _first_arg(a, "path", "file")
        return f"Wrote {_basename(p)}" if p else "Wrote a file"
    if name in ("patch",):
        p = _first_arg(a, "path", "file")
        return f"Edited {_basename(p)}" if p else "Edited a file"
    if name in ("search_files",):
        pat = _first_arg(a, "pattern")
        return f'Searched files for "{_short(pat)}"' if pat else "Searched files"

    # --- shell / process ----------------------------------------------------
    if name in ("terminal", "execute_command", "run_command"):
        cmd = _first_arg(a, "command", "cmd")
        return f"Ran `{_short(cmd, 60)}`" if cmd else "Ran a shell command"
    if name in ("process",):
        act = _first_arg(a, "action")
        return f"Managed a process ({_short(act, 20)})" if act else "Managed a process"

    # --- memory / skills ----------------------------------------------------
    if name == "memory":
        act = _first_arg(a, "action")
        verb = {"add": "Saved a memory", "replace": "Updated a memory",
                "remove": "Removed a memory"}.get(str(act), "Updated memory")
        return verb
    if name == "skill_manage":
        ops = a.get("operations")
        if isinstance(ops, list) and ops:
            n = ops[0].get("name") if isinstance(ops[0], dict) else None
            act = ops[0].get("action") if isinstance(ops[0], dict) else None
            if n and act:
                return f"{str(act).capitalize()}d skill '{_short(n, 30)}'"
        return "Managed skills"
    if name in ("skill_view",):
        n = _first_arg(a, "name")
        return f"Viewed skill '{_short(n, 30)}'" if n else "Viewed a skill"

    # --- delegation / tasks ---------------------------------------------------
    if name == "delegate_task":
        tasks = a.get("tasks")
        if isinstance(tasks, list) and tasks:
            return f"Delegated {len(tasks)} subagent task{'s' if len(tasks) != 1 else ''}"
        return "Delegated a task"
    if name == "todo":
        return "Updated the task list"
    if name == "cronjob":
        act = _first_arg(a, "action")
        return f"{str(act).capitalize()}d a scheduled job" if act else "Managed a scheduled job"

    # --- desktop / GUI --------------------------------------------------------
    if name == "desktop_preview":
        act = _first_arg(a, "action")
        return f"{str(act).capitalize()}ed the preview pane" if act else "Used the preview pane"
    if name in ("read_file",):
        return "Read a file"

    # --- fallback: honest, never a guess --------------------------------------
    return f"Used {name}"


def summarize_event(action_type: str, tool_name: Optional[str],
                    detail: Optional[dict], args: Optional[dict] = None) -> str:
    """Route to the right summarizer by action type."""
    if action_type in ("tool_call", "skill_write"):
        return summarize_tool_call(tool_name, args)
    if action_type == "llm_call":
        model = (detail or {}).get("model")
        return f"Thought with {_short(model, 40)}" if model else "Thought"
    if action_type == "message":
        return "Replied to you"
    if action_type == "session_start":
        return "Session started"
    if action_type == "session_end":
        return "Session ended"
    if action_type == "approval_request":
        return "Asked for your approval"
    if action_type == "approval_granted":
        return "You approved an action"
    if action_type == "error":
        return "Something went wrong"
    return action_type.replace("_", " ").capitalize()
