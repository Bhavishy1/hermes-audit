"""hermes-audit: plugin entry point for the Hermes audit trail.

Registers execution middleware (tool + LLM envelopes) and lifecycle
observer hooks that feed a single AuditJournal writing to
$HERMES_HOME/audit.db. Every registration is individually guarded so a
partial failure logs a warning instead of killing plugin load.
"""

from __future__ import annotations

import atexit
import logging
import os
import sys

# Hermes loads this __init__.py as an isolated top-level module WITHOUT the
# plugin directory on sys.path (verified: "No module named 'schema'"). Put the
# plugin's own dir on sys.path FIRST so the sibling modules (schema, journal,
# envelope, hooks) import as plain top-level modules. This must run before
# those imports.
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from schema import DDL  # noqa: F401,E402  (DDL re-exported for tooling/tests)
from journal import AuditJournal  # noqa: E402
import envelope  # noqa: E402
import hooks  # noqa: E402

logger = logging.getLogger("hermes_audit")

# The single writer instance created (at most once) by register().
_journal = None


def _resolve_db_path() -> str:
    """Return $HERMES_HOME/audit.db, falling back to ~/.hermes/audit.db."""
    hermes_home = None
    try:
        from hermes_constants import get_hermes_home

        hermes_home = get_hermes_home()
    except Exception as exc:  # ImportError, API drift, anything at all
        logger.warning(
            "hermes-audit: could not resolve Hermes home via "
            "hermes_constants.get_hermes_home() (%s); falling back to "
            "~/.hermes",
            exc,
        )
    if not hermes_home:
        hermes_home = os.path.expanduser("~/.hermes")
    return os.path.join(hermes_home, "audit.db")


def register(ctx) -> None:
    """Plugin entry point: wire the audit journal, middleware, and hooks."""
    global _journal

    db_path = _resolve_db_path()
    try:
        _journal = AuditJournal(db_path)
    except Exception as exc:
        logger.error("hermes-audit: failed to create AuditJournal at %s: %s",
                     db_path, exc)
        _journal = None
        return
    logger.info("hermes-audit: journal created at %s", db_path)

    # T1: drain + close the journal when the gateway process exits, so
    # queued tail events (session_end, final tool completions) survive.
    # _shutdown() is idempotent (closes once, _journal=None), so a
    # double registration (or a manual close) is harmless.
    try:
        atexit.register(_shutdown)
    except Exception as exc:
        logger.warning("hermes-audit: atexit.register(_shutdown) failed: %s", exc)

    # Hermes-side alternative: PluginRegistration._on_dispose
    # (hermes_cli/plugins.py ~1252) is host-owned — set via field(init=False)
    # and fired by PluginRegistration.dispose() when the plugin manager
    # unloads the plugin. It is not exposed through PluginContext.register(),
    # so a plugin cannot wire it directly today. If that surface widens,
    # register a dispose callback that calls _shutdown() so unload
    # (not just process exit) drains the writer; atexit remains the
    # process-exit safety net either way.

    try:
        envelope.set_journal(_journal)
    except Exception as exc:
        logger.warning("hermes-audit: envelope.set_journal failed: %s", exc)
    try:
        hooks.set_journal(_journal)
    except Exception as exc:
        logger.warning("hermes-audit: hooks.set_journal failed: %s", exc)

    # -- middleware (behavioral envelopes) --------------------------------
    try:
        ctx.register_middleware("tool_execution",
                                envelope.tool_execution_envelope)
        logger.info("hermes-audit: registered tool_execution middleware")
    except Exception as exc:
        logger.warning("hermes-audit: registering tool_execution middleware "
                       "failed: %s", exc)

    try:
        ctx.register_middleware("llm_execution",
                                envelope.llm_execution_envelope)
        logger.info("hermes-audit: registered llm_execution middleware")
    except Exception as exc:
        logger.warning("hermes-audit: registering llm_execution middleware "
                       "failed: %s", exc)

    # -- lifecycle observer hooks -----------------------------------------
    # PluginContext.register_hook(hook_name, callback) — verified against
    # hermes_cli/plugins.py (PluginContext.register_hook, line ~3387).
    hook_bindings = (
        ("on_session_start", hooks.on_session_start),
        ("on_session_end", hooks.on_session_end),
        ("subagent_start", hooks.on_subagent_start),
        ("subagent_stop", hooks.on_subagent_stop),
        ("pre_approval_request", hooks.pre_approval_request),
        ("post_approval_response", hooks.post_approval_response),
        # Tool-call ATTEMPTS before execution (C1): pre_tool_call is fired
        # before a tool runs, including calls that are then vetoed/blocked —
        # those never reach tool_execution middleware. Observer is fail-open
        # (never raises, always returns None) so it cannot veto tools.
        ("pre_tool_call", hooks.on_pre_tool_call),
        # Assistant's final text reply, once per turn (turn_finalizer.py).
        ("post_llm_call", hooks.message_capture),
    )
    for hook_name, callback in hook_bindings:
        try:
            ctx.register_hook(hook_name, callback)
            logger.info("hermes-audit: registered hook %s", hook_name)
        except Exception as exc:
            logger.warning("hermes-audit: registering hook %s failed: %s",
                           hook_name, exc)


def _shutdown() -> None:
    """Flush and close the journal (call on process exit / plugin unload)."""
    global _journal
    if _journal is None:
        return
    try:
        _journal.flush()
    except Exception as exc:
        logger.warning("hermes-audit: journal flush failed: %s", exc)
    try:
        _journal.close()
    except Exception as exc:
        logger.warning("hermes-audit: journal close failed: %s", exc)
    _journal = None
    logger.info("hermes-audit: journal shut down")
