"""
Validation Agent - Chat UI
Main entry point.
"""

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ensure the project root is on ``sys.path`` before any lazy imports
# fire. Chainlit primes ``sys.path`` for the initial ``import app``
# but the entry does NOT reliably persist for later async handlers,
# so lazy imports inside handlers (e.g. ``from prompts.skills.run
# import ...`` in ``handle_run``, or ``from prompts.skills.compare
# import ...`` in the compare button callbacks) would otherwise fail
# with ``ModuleNotFoundError: No module named 'prompts'`` on first use.
#
# Belt-and-suspenders: we insert at import time (below) AND provide
# :func:`_ensure_project_root_on_path` which the handlers call inline
# right before their lazy imports, so the priming survives whatever
# Chainlit does to ``sys.path`` between app.py import and message
# handling.
_PROJECT_ROOT = str(Path(__file__).resolve().parent)


def _ensure_project_root_on_path() -> None:
    """Idempotently re-insert the project root into ``sys.path``.

    Called from inside async handlers immediately before any lazy
    ``from prompts.skills.<op> import ...`` statement. Also invoked
    once at module import time (below) for early handlers that never
    ran through the shared sub-agent engine.
    """
    if _PROJECT_ROOT not in sys.path:
        sys.path.insert(0, _PROJECT_ROOT)


_ensure_project_root_on_path()

import chainlit as cl
import asyncio
import config
from agents.analysis_agent import get_analysis_agent
from workflows.session_manager import get_session_manager
from utils.logger import get_logger
from utils.progress import ProgressCard
from models.session import StepStatus
from agents.validation_agent import get_orchestrator, load_reporting_skill, load_validate_skill
from services.llm_service import get_llm_service
from tools.workflow_tools import register_many
from tools.auto_apply import apply_fix_to_file, apply_fixes_to_file
from tools.xrf_tools import update_xrf_from_cs_edit

# Per-operation vertical modules are self-contained inside
# ``prompts/skills/<op>/``. Their implementation classes are NOT
# imported here — each SKILL.md declares its own ``entrypoint:`` and
# the orchestrator resolves + invokes it via
# :meth:`Orchestrator.run_skill`. Keeping this layer ignorant of
# skill internals is what makes a skill folder truly portable: drop
# ``prompts/skills/copy/`` into another project and nothing here needs
# to change.

logger = get_logger("app")


# ── Provider icons + display order (cosmetic, used in the model selector) ─
_PROVIDER_META = {
    "openai":    {"icon": "🟢", "label": "OpenAI",            "order": 0},
    "anthropic": {"icon": "🟠", "label": "Anthropic Claude",  "order": 1},
    "google":    {"icon": "🔵", "label": "Google Gemini",     "order": 2},
}

# Sentinel value put into the dropdown for "section header" rows. If the
# user accidentally selects one of these, on_settings_update ignores it.
_HEADER_SENTINEL_PREFIX = "__header__:"


def _provider_icon_for(provider_key: str) -> str:
    return _PROVIDER_META.get(provider_key, {}).get("icon", "⚪")


def _provider_label_for(provider_key: str, fallback: str = "") -> str:
    return _PROVIDER_META.get(provider_key, {}).get(
        "label", fallback or str(provider_key).title()
    )


def _build_model_select():
    """Build the Chainlit Select widget for the model picker.

    The dropdown is a single flat list (Chainlit doesn't support nested
    submenus in ChatSettings), but it's visually grouped by provider:

        ━━━ 🟢 OpenAI ━━━
            GPT-4.1
            GPT-5.4
        ━━━ 🟠 Anthropic Claude ━━━
            Claude Opus 4.8
            Claude Sonnet 4.6
            ...
        ━━━ 🔵 Google Gemini ━━━
            Gemini 3 Pro (preview)

    Models come from prompts/model_registry.yaml — adding/removing one
    there immediately reflects here (no code change).
    """
    from chainlit.input_widget import Select

    llm = get_llm_service()
    grouped = llm.get_models_by_provider() or {}

    # Sort providers by configured order, unknowns last.
    sorted_providers = sorted(
        grouped.items(),
        key=lambda kv: _PROVIDER_META.get(kv[0], {}).get("order", 99),
    )

    # Build dict[label -> value]. Section headers map to a sentinel value
    # so we can detect & ignore them in on_settings_update.
    items: dict[str, str] = {}
    header_idx = 0
    for provider_key, models in sorted_providers:
        if not models:
            continue
        icon = _provider_icon_for(provider_key)
        plabel = _provider_label_for(provider_key)
        header_idx += 1
        header_display = f"━━━━━━  {icon}  {plabel}  ━━━━━━"
        items[header_display] = f"{_HEADER_SENTINEL_PREFIX}{header_idx}"
        for entry in models:
            if not isinstance(entry, dict):
                continue
            mid = entry.get("id")
            if not mid:
                continue
            mlabel = entry.get("label", mid)
            display = f"        {icon}  {mlabel}"
            if display in items:
                display = f"{display}   ({mid[-12:]})"
            items[display] = mid

    current = llm.get_current_model()

    # Fallback if registry is empty for any reason
    if not items:
        items = {f"⚪  {current}": current}

    # Ensure current model is selectable even if it wasn't in the registry
    if current not in items.values():
        items[f"⚪  {current}  (from .env)"] = current

    # Initial value = the currently active model, so opening the panel
    # always reflects what's actually in use. The "Reset" button is
    # rewired client-side (public/model-picker.js) to revert to the
    # configured DEFAULT_MODEL from .env — Chainlit's built-in Reset
    # only undoes in-dialog edits, it does NOT revert to initial_value.
    # We embed the env default in the description so the JS can read it.
    import os
    default_model = os.getenv("DEFAULT_MODEL") or current

    return Select(
        id="active_model",
        label="🤖  LLM Models. Switch Any time",
        items=items,
        initial_value=current,
        description=f"Choose the model for this session. Reset → {default_model}",
    )


async def _register_chat_settings():
    """Render the gear-icon settings panel with the model selector."""
    try:
        settings = cl.ChatSettings([_build_model_select()])
        await settings.send()
    except Exception as e:
        # Never let UI sugar break the chat session.
        logger.warning(f"Could not render ChatSettings (model picker): {e}")

session_manager = get_session_manager()


# ── Handler output capture ───────────────────────────────────────────
# Wraps cl.Message.send so every message a handler sends during
# execution is silently recorded in cl.user_session["_handler_output"].
# This lets follow-up questions ("give me a summary") include the
# actual execution details (timing, comparison results, ticket numbers)
# without modifying any handler code.
_original_cl_msg_send = cl.Message.send


async def _capturing_cl_msg_send(self):
    result = await _original_cl_msg_send(self)
    try:
        buf = cl.user_session.get("_handler_output")
        if buf is not None and self.content:
            # Skip invisible pipeline-HUD markers so the LLM history stays
            # clean (they are UI-only, meaningless to the model).
            if "va-pipeline?" not in self.content:
                buf.append(self.content)
    except Exception:
        pass  # user_session not available (e.g. during startup)
    return result


cl.Message.send = _capturing_cl_msg_send


# ── Pipeline HUD marker emitter ──────────────────────────────────────
# Chainlit's markdown pipeline escapes raw <div> HTML into literal text
# (so you'd see the markup instead of an invisible element). Images ARE
# rendered as real <img> DOM nodes though, so we encode the pipeline
# event payload into an image's `alt` attribute using a URL-query style:
#
#     ![va-pipeline?event=step_start&num=2&op=load](data:,)
#
# public/pipeline-hud.js watches for `img[alt^="va-pipeline?"]` via a
# MutationObserver and updates the floating vertical HUD panel.
# public/aurora-premium.css hides both the <img> and its wrapping chat
# message so nothing is visible in the chat.
#
# Zero impact on backend / workflow logic — if pipeline-hud.js fails to
# load for any reason, the images are simply invisible no-ops and the
# app behaves exactly as before.
async def _emit_pipeline_event(event: str, **attrs) -> None:
    """Emit an invisible pipeline-HUD marker for public/pipeline-hud.js."""
    from urllib.parse import quote

    params = [f"event={quote(event)}"]
    for key, value in attrs.items():
        if value is None:
            continue
        params.append(f"{key}={quote(str(value))}")
    query = "&".join(params)
    # Markdown image with a 1x1 transparent GIF as the src (fallback if
    # CSS doesn't hide it — no broken-image icon).
    src = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
    marker = f"![va-pipeline?{query}]({src})"
    try:
        await cl.Message(content=marker).send()
    except Exception:
        # Never let a UI-only marker break the workflow.
        pass


def _msg(key: str, **kwargs) -> str:
    """Shortcut for skill-file message lookup (see WORKFLOW_ORCHESTRATION_SKILL.md)."""
    return get_orchestrator().get_message(key, **kwargs)


def _build_rich_history(
    tool: str | None,
    args: str,
    baseline: str | None,
    handler_output: list[str] | None = None,
) -> str:
    """Build a rich conversation history entry after a tool execution.

    Combines three data sources so the LLM can answer follow-up questions
    accurately:
      1. Which tool was called and with what arguments
      2. Session state (step-by-step status from SessionManager)
      3. Actual handler output messages captured during execution
    """
    if not tool:
        return "Responded with a conversational answer (no tool was called)."

    parts = [f"Executed tool '{tool}'"]
    if args:
        parts[0] += f" with args '{args}'"
    if baseline:
        parts[0] += f" for baseline {baseline}"

    # ── Actual handler output (captured via _capturing_cl_msg_send) ──
    if handler_output:
        combined = "\n---\n".join(handler_output[-20:])
        # Cap total size to avoid bloating context.
        if len(combined) > 5000:
            combined = combined[-5000:]
        parts.append(f"Execution output:\n{combined}")

    # ── Session state enrichment ─────────────────────────────────────
    if baseline:
        try:
            summary = session_manager.get_session_summary(baseline)
            steps = summary.get("steps", {})
            completed = summary.get("completed", 0)
            failed = summary.get("failed", 0)
            total = summary.get("total_steps", 0)
            next_step = summary.get("next_step")

            parts.append(
                f"Session state: {completed}/{total} steps completed"
                + (f", {failed} failed" if failed else "")
                + (f". Next pending step: {next_step}" if next_step else ". All steps done!")
            )

            step_lines = []
            for step_name, status in steps.items():
                icon = {"completed": "✅", "failed": "❌", "pending": "⏳", "running": "🔄"}.get(status, "⏳")
                step_lines.append(f"  {icon} {step_name}: {status}")
            if step_lines:
                parts.append("Step details:\n" + "\n".join(step_lines))
        except Exception:
            pass  # Session not found — skip enrichment.

    return "\n".join(parts)

@cl.on_chat_start
async def on_chat_start():
    # Chainlit fires this on every websocket connect — including browser
    # refreshes and network reconnects, not just true "new chat" clicks.
    # We mark the user_session the first time we render the welcome so
    # subsequent reconnects (within `session_timeout` from
    # `.chainlit/config.toml`) are silent and keep all in-memory state
    # (agentic_history, chat_history, active program/baseline). A real
    # reset only happens when the user clicks "New Chat" in the UI
    # (which `confirm_new_chat = true` already guards).
    if cl.user_session.get("welcomed"):
        logger.info("Chat session reconnected — preserving state, no welcome")
        return

    logger.info("New chat session started")
    cl.user_session.set("welcomed", True)

    # Render the ⚙ settings panel with the model selector (non-blocking,
    # purely additive — does not change existing welcome flow).
    await _register_chat_settings()

    # Welcome markdown lives in prompts/welcome.md (referenced from the
    # skill file as `ui.welcome_file`). The orchestrator loads and caches
    # it at startup, so this is a pure in-memory lookup per session.
    welcome = get_orchestrator().get_welcome_text()
    await cl.Message(content=welcome).send()


@cl.on_chat_resume
async def on_chat_resume(thread):
    # Fires when Chainlit's data layer resumes a previous thread (e.g. user
    # navigates back into a saved chat). We deliberately do nothing here:
    # the user is continuing an existing conversation, so re-rendering the
    # welcome or resetting any session state would be wrong.
    logger.info("Chat session resumed — no welcome, state preserved")
    cl.user_session.set("welcomed", True)
    # Re-render the settings panel so the model picker is still available
    # on resumed sessions (Chainlit drops widget state on reconnect).
    await _register_chat_settings()


@cl.on_settings_update
async def on_settings_update(settings: dict):
    """Apply UI changes from the ⚙ settings panel.

    Currently handles only the model selector. We intentionally keep this
    handler tiny and defensive so it can never break a chat session.
    """
    new_model = (settings or {}).get("active_model")
    if not new_model:
        return

    # Ignore clicks on the "━━━ Provider ━━━" section header rows.
    if isinstance(new_model, str) and new_model.startswith(_HEADER_SENTINEL_PREFIX):
        await cl.Message(
            content=(
                "ℹ️ That's a **section header**, not a model. "
                "Open the ⚙ settings panel again and pick an indented "
                "model name underneath the header."
            ),
            author="System",
        ).send()
        return

    llm = get_llm_service()
    previous = llm.get_current_model()
    if new_model == previous:
        return

    try:
        llm.set_model(new_model)
        cl.user_session.set("active_model", new_model)
        logger.info(f"User switched model: {previous} → {new_model}")
    except Exception as e:
        logger.error(f"Failed to switch model to {new_model}: {e}")
        await cl.Message(
            content=f"⚠️ Could not switch model to `{new_model}`: {e}",
            author="System",
        ).send()


@cl.on_message
async def on_message(message: cl.Message):
    content = message.content.strip()
    logger.info(f"Received: {content}")

    # /add-issue wizard — session-scoped multi-step form for curated issues.
    if cl.user_session.get("add_issue_state") is not None:
        await _add_issue_wizard_step(content)
        return
    if content.lower().startswith("/add-issue"):
        await _add_issue_wizard_start()
        return
    # The client-side modal (public/add-issue-form.js) opens without a
    # server round-trip and then submits the full template as one chat
    # message. Recognise that template by its unique marker pair and
    # route it into the wizard even though ``add_issue_state`` is unset.
    if (
        "===== BUGGY CODE =====" in content
        and "===== FIXED CODE =====" in content
    ):
        cl.user_session.set("add_issue_state", {"awaiting_paste": True})
        await _add_issue_wizard_step(content)
        return

    # ── Autopilot (separate, opt-in feature) ─────────────────────────
    # Active only after the dashboard Autopilot button (sends "/autopilot")
    # or while an Autopilot session is live. When inactive this is a no-op
    # and the normal routing below runs byte-identically.
    if content.lower().startswith("/autopilot") or cl.user_session.get("autopilot_active"):
        await handle_autopilot_turn(message)
        return

    # All routing is driven by prompts/WORKFLOW_ORCHESTRATION_SKILL.md
    # via services/orchestrator.py.  Adding a new command requires only a
    # skill-file entry plus a registered handler — no edits in this file.
    orchestrator = get_orchestrator()

    # ── Conversation memory ──────────────────────────────────────────
    # Retrieve prior turns so the LLM can resolve references like
    # "it", "now load", "that baseline".  Stored per Chainlit session.
    agentic_history = cl.user_session.get("agentic_history") or []

    # Start collecting handler output so follow-up questions can
    # reference the actual execution details (timing, results, etc.).
    cl.user_session.set("_handler_output", [])

    result = await orchestrator.agentic_dispatch(content, conversation_history=agentic_history)

    # Retrieve captured handler output and stop collecting.
    handler_output = cl.user_session.get("_handler_output") or []
    cl.user_session.set("_handler_output", None)

    # Append this turn to history.  Each turn is a user message +
    # a rich assistant summary (including actual handler output) so the
    # LLM can answer follow-up questions accurately.
    agentic_history.append({"role": "user", "content": content})
    if result:
        tool = result.get("tool")
        args = result.get("args", "")
        baseline = result.get("baseline")
        # Only include handler output for tool executions, not for
        # LLM text responses (which would just duplicate content).
        output = handler_output if tool else None
        summary = _build_rich_history(tool, args, baseline, output)
        agentic_history.append({"role": "assistant", "content": summary})

    # Cap history to avoid unbounded growth (matches skill-file config).
    max_turns = 20  # store pairs; the LLM receives max_history_turns from this
    cl.user_session.set("agentic_history", agentic_history[-(max_turns * 2):])



async def handle_chat(content: str):
    """Route a free-form natural-language message to the chat agent.

    Maintains a small per-session history and the last detected program /
    baseline so multi-turn conversations stay coherent. Never raises — all
    errors degrade to a user-visible message.
    """
    # Lazy import keeps app start-up fast and avoids a hard dependency on the
    # chat prompt files being present until the feature is first used.
    from agents.chat_agent import ChatTurn, get_chat_agent

    history_raw = cl.user_session.get("chat_history") or []
    history = [
        ChatTurn(role=item.get("role", "user"), content=item.get("content", ""))
        for item in history_raw
        if isinstance(item, dict)
    ]
    active_program = cl.user_session.get("chat_active_program")
    active_baseline = cl.user_session.get("chat_active_baseline")

    # Stream tokens into a live Chainlit message (ChatGPT/Copilot style).
    # The message is created empty, then each token is appended via
    # `stream_token` as it arrives from the LLM. The final content is
    # whitespace-trimmed before the closing update.
    msg = cl.Message(content="")
    await msg.send()

    try:
        agent = get_chat_agent()
        response = await agent.chat_stream(
            user_message=content,
            history=history,
            active_program=active_program,
            active_baseline=active_baseline,
            on_token=msg.stream_token,
        )
        reply_text = response.text
        program_name = response.program_name
        baseline_name = response.baseline_name
    except Exception as exc:
        logger.error(f"Chat agent error: {exc}")
        reply_text = (
            f"❌ Chat assistant error: `{exc}`\n\n"
            "Existing commands (`help`, `list`, `validate`, ...) still work."
        )
        program_name = active_program
        baseline_name = active_baseline

    # Persist conversation state (cap history at 12 messages).
    history_raw.append({"role": "user", "content": content})
    history_raw.append({"role": "assistant", "content": reply_text})
    cl.user_session.set("chat_history", history_raw[-12:])
    if program_name:
        cl.user_session.set("chat_active_program", program_name)
    if baseline_name:
        cl.user_session.set("chat_active_baseline", baseline_name)

    # Final trim + flush: replaces streamed content with the canonical,
    # whitespace-trimmed reply so any trailing tokens / partial chunks
    # are normalised before the message is sealed.
    msg.content = (reply_text or "").strip()
    await msg.update()


async def handle_help():
    help_text = """
# 📖 Help

## Step Commands
| Command | Step | Description |
|---------|------|-------------|
| `copy <baseline>` | 1 | Copy baseline files |
| `load <baseline>` | 2 | Load data to DB |
| `branch <baseline>` | 3a | Create branch from master |
| `run <baseline>` | 3b | Run C# code (dotnet run) |
| `unload <baseline>` | 5 | Unload from DB |
| `compare <baseline>` | 6 | Compare output files |
| `report <baseline>` | 7 | Generate report |
| `finalize <baseline>` | 8 | Push code & create PR |

## Other Commands
| Command | Description |
|---------|-------------|
| `validate <baseline>` | Run all steps |
| `status <baseline>` | Show progress |
| `reset <baseline>` | Reset all steps |
| `list` | List all sessions |

## Example
"""
    await cl.Message(content=help_text).send()


async def handle_list():
    sessions = session_manager.list_sessions()
    
    if not sessions:
        await cl.Message(content=_msg("list.empty")).send()
        return
    
    lines = [_msg("list.header")]
    for baseline in sessions:
        summary = session_manager.get_session_summary(baseline)
        status = "✅ Done" if summary['is_done'] else f"🔄 {summary['completed']}/{summary['total_steps']}"
        lines.append(f"- `{baseline}` ({summary['group']}) - {status}")
    
    await cl.Message(content="\n".join(lines)).send()


async def handle_status(baseline: str):
    if not baseline:
        await cl.Message(content=_msg("usage.status")).send()
        return
    
    status_display = session_manager.format_status_display(baseline)
    await cl.Message(content=status_display).send()


async def handle_reset(baseline: str):
    if not baseline:
        await cl.Message(content=_msg("usage.reset")).send()
        return
    
    session = session_manager.get_session(baseline)
    session.reset_all()
    await cl.Message(content=_msg("reset.success", baseline=baseline)).send()


async def handle_copy(baseline: str):
    """Step 1: Copy baseline from G: to C:

    The chat-UI plumbing (argument validation, progress card, success /
    failure messages) lives here. The actual copy logic is owned by
    the ``copy`` skill (``prompts/skills/copy/SKILL.md``); we invoke
    it through the orchestrator's generic ``run_skill`` entry so this
    layer never imports any class, module, or file from inside the
    skill folder.
    """
    if not baseline:
        await cl.Message(content=_msg("usage.copy")).send()
        return
    
    # Resolve the session, then invoke the skill via the orchestrator.
    # The skill folder owns CopyAgent and the four tool calls — we
    # only see the (success, message) tuple it returns.
    session = session_manager.get_session(baseline)
    orchestrator = get_orchestrator()

    # Run the (blocking-heavy) skill in a thread pool with a live
    # progress card so chainlit's event loop stays responsive.
    loop = asyncio.get_event_loop()
    future = loop.run_in_executor(
        None,
        lambda: asyncio.run(orchestrator.run_skill("copy", session)),
    )

    async with ProgressCard(
        "Step 1: Copy Baseline", baseline, interval=10, eta_seconds=120
    ) as card:
        await card.wait_for(future, status="Copying input files...")
        success, message = await future
        if success:
            await card.complete("Copy complete")
        else:
            await card.fail(message or "Failed")

    if success:
        await cl.Message(content=_msg("copy.success", baseline=baseline)).send()
    else:
        await cl.Message(content=_msg("copy.failure", error=message)).send()

async def handle_branch(baseline: str):
    """Step 3a: Create branch and pull to local.

    Dispatches directly through the orchestrator into the
    self-contained ``prompts/skills/branch/`` skill — no intermediate
    agent wrapper. The skill returns the same ``(success, result_dict)``
    shape ``DevOpsAgent.run_step_3a_branch`` used to return, so the UI
    rendering below is unchanged.
    """
    if not baseline:
        await cl.Message(content=_msg("usage.branch")).send()
        return
    
    # Show starting message
    await cl.Message(content=_msg("branch.starting", baseline=baseline)).send()
    
    try:
        # Invoke the branch skill directly — skip DevOpsAgent (Step 3a
        # no longer lives there). The skill's ``run(session)`` is async
        # and off-loads its blocking Azure DevOps calls internally.
        from agents.validation_agent import get_orchestrator
        session = session_manager.get_session(baseline)
        success, result = await get_orchestrator().run_skill("branch", session)
        
        if success:
            branch_name = result.get("branch_name", "")
            branch_url = result.get("branch_url", "")
            ticket_id = result.get("ticket_id")
            ticket_url = result.get("ticket_url", "")
            
            # Build ticket link if available
            ticket_link = ""
            if ticket_id and ticket_url:
                ticket_link = f"\n• 🎫 [Ticket {ticket_id}]({ticket_url})"
            
            summary = f"""✅ **Step 3a: Complete!**

Branch ready for `{baseline}`

**What was done:**
• ✅ Branch created/verified
• ✅ Code pulled to local
• ✅ Ready to run

**Links:**
• 🔗 [Branch: {branch_name}]({branch_url}){ticket_link}

**Next Steps:**
• Run `run {baseline}` (Step 3b)"""

            await cl.Message(content=summary).send()
        else:
            error_msg = result.get("message", "Unknown error")
            await cl.Message(content=f"❌ **Step 3a: Failed**\n\n{error_msg}").send()
            
    except Exception as e:
        await cl.Message(content=f"❌ **Step 3a: Unexpected Error**\n\n```\n{str(e)}\n```").send()
        logger.error(f"Unexpected error in handle_branch: {e}")

async def handle_run(baseline: str):
    """Step 3b: Run C# program.

    Dispatches directly through the orchestrator into the
    self-contained ``prompts/skills/run/`` skill — no intermediate
    agent wrapper. The pre-flight ``extract_program_name`` /
    ``find_program_file`` checks are imported from the same skill's
    facade so the UI can short-circuit with a friendly "Program Not
    Found" message before dispatching.
    """
    if not baseline:
        await cl.Message(content=_msg("usage.run")).send()
        return
    
    # Ensure ``prompts.skills.run`` is importable regardless of what
    # Chainlit did to ``sys.path`` after app.py loaded.
    _ensure_project_root_on_path()
    from prompts.skills.run import extract_program_name, find_program_file
    
    program_name = extract_program_name(baseline)

    # Check if program exists first
    found, path_or_msg = find_program_file(program_name)
    if not found:
        await cl.Message(content=f"❌ **Step 3b: Program Not Found**\n\n{path_or_msg}\n\n💡 Create the program first, then run again.").send()
        return

    try:
        # Invoke the run skill directly — no ScriptRunnerAgent wrapper.
        # The skill's ``run(session)`` is async and off-loads its
        # blocking ``dotnet run`` subprocess internally via
        # ``asyncio.to_thread``.
        session = session_manager.get_session(baseline)
        future = asyncio.ensure_future(
            get_orchestrator().run_skill("run", session)
        )

        async with ProgressCard(
            f"Step 3b: Run {program_name}", baseline, interval=15, eta_seconds=180
        ) as card:
            await card.wait_for(future, status=f"Executing `{path_or_msg}`...")
            success, result = await future
            if success:
                await card.complete("Run complete")
            else:
                err = (result.get("error_info") or {}).get("error_message", "Failed")
                await card.fail(err)
        
        if success:
            # Get output preview
            output_text = result.get("output") or ""
            output_preview = output_text[:1500] if output_text else "No output"
            if len(output_text) > 1500:
                output_preview += "\n... (truncated)"
            
            # Check for warnings
            error_info = result.get("error_info") or {}
            warnings = error_info.get("warnings", [])
            warning_text = ""
            if warnings:
                warning_text = f"\n\n**Warnings ({len(warnings)}):**\n```\n" + "\n".join(warnings[:5]) + "\n```"
            
            summary = f"""✅ **Step 3b: Complete!**

Program `{program_name}` executed successfully.
{warning_text}

**Output:**
**Next Steps:**
• Run `unload {baseline}` (Step 5)
• Then `compare {baseline}` (Step 6)"""
            
            await cl.Message(content=summary).send()
        else:
            error_info = result.get("error_info") or {}
            error_type = error_info.get("error_type", "unknown")
            error_msg = error_info.get("error_message", "Unknown error")
            stack_trace = error_info.get("stack_trace", "")
            sql_error = error_info.get("sql_error", "")
            exit_code = result.get("exit_code", 1)
            
            # Build error display
            error_display = f"**Error Type:** `{error_type}`\n\n"
            
            if sql_error:
                error_display += f"**SQL Error:**\n```\n{sql_error}\n```\n\n"
            
            if error_msg:
                error_display += f"**Message:**\n```\n{error_msg}\n```\n\n"
            
            if stack_trace:
                error_display += f"**Stack Trace:**\n```\n{stack_trace}\n```\n\n"
            
            summary = f"""❌ **Step 3b: Failed**

Program `{program_name}` failed.

{error_display}

**Next Steps:**
• Fix the error manually
• Or click "Analyse with AI" for automated analysis
• Then run `run {baseline}` again"""
            
            # Create AI analysis button
            actions = [
                cl.Action(
                    name="analyse_with_ai",
                    payload={
                        "baseline": baseline,
                        "program_name": program_name,
                        "error_message": error_msg,
                        "error_type": error_type,
                        "stack_trace": stack_trace,
                        "exit_code": exit_code
                    },
                    label="🤖 Analyse with AI"
                )
            ]
            
            await cl.Message(content=summary, actions=actions).send()
            
    except Exception as e:
        await cl.Message(content=f"❌ **Step 3b: Unexpected Error**\n\n```\n{str(e)}\n```").send()
        logger.error(f"Unexpected error in handle_run: {e}")   
        
        

async def handle_load(baseline: str):
    """Step 2: Load data to DB"""
    if not baseline:
        await cl.Message(content=_msg("usage.load")).send()
        return
    
    # Show starting message
    msg = cl.Message(content=_msg("load.starting", baseline=baseline))
    await msg.send()
    
    
    await asyncio.sleep(0.5)
    
    # Invoke the load skill directly through the orchestrator — no
    # ScriptRunnerAgent wrapper. Skill returns (bool, str).
    session = session_manager.get_session(baseline)
    success, message = await get_orchestrator().run_skill("load", session)
    
    if success:
        await cl.Message(content=_msg("load.success", baseline=baseline)).send()
    else:
        await cl.Message(content=_msg("load.failure", error=message)).send()
 


async def handle_unload(baseline: str):
    """Step 5: Unload from DB"""
    if not baseline:
        await cl.Message(content=_msg("usage.unload")).send()
        return
    
    # Show starting message
    msg = cl.Message(content=_msg("unload.starting", baseline=baseline))
    await msg.send()
    
    
    await asyncio.sleep(0.5)
    
    # Invoke the unload skill directly through the orchestrator — no
    # ScriptRunnerAgent wrapper. Skill returns (bool, str).
    session = session_manager.get_session(baseline)
    success, message = await get_orchestrator().run_skill("unload", session)
    
    if success:
        await cl.Message(content=_msg("unload.success", baseline=baseline)).send()
    else:
        await cl.Message(content=_msg("unload.failure", error=message)).send()


async def handle_compare(baseline_name: str):
    """Handle Step 6: Compare output.

    Dispatches directly through the orchestrator into the
    self-contained ``prompts/skills/compare/`` skill — no intermediate
    agent wrapper. The skill returns the same ``(success, result_dict)``
    shape ``CompareAgent.run_step_6_compare`` used to return, so the UI
    rendering below is unchanged.
    """
    if not baseline_name:
        await cl.Message(content=_msg("usage.compare")).send()
        return
    
    session = session_manager.get_session(baseline_name)

    # Word-popup warning shown once before the live progress card
    await cl.Message(content=_msg("compare.word_popup_note")).send()

    try:
        # Invoke the compare skill directly — skip CompareAgent (Step 6
        # no longer lives there). The skill's ``run(session)`` is async
        # and off-loads its blocking pwsh subprocess internally via
        # ``asyncio.to_thread``.
        future = asyncio.ensure_future(
            get_orchestrator().run_skill("compare", session)
        )

        async with ProgressCard(
            "Step 6: Compare Output", baseline_name, interval=15, eta_seconds=180
        ) as card:
            await card.wait_for(future, status="Comparing & generating report...")
            success, result = await future
            if success:
                await card.complete("Compare complete")
            else:
                await card.fail(result.get("message") or "Compare failed")

        if not success:
            await cl.Message(content=f"❌ **Step 6: Compare Failed**\n\n{result.get('message')}").send()
            return
        
        # Build result message
        all_pass = result.get("all_pass", False)
        csv_pass = result.get("after_csv_pass", 0)
        csv_fail = result.get("after_csv_fail", 0)
        root_pass = result.get("root_mixed_pass", 0)
        root_fail = result.get("root_mixed_fail", 0)
        failed_files = result.get("failed_files", [])
        report_path = result.get("report_path", "")
        
        if all_pass:
            # All passed - no approval needed
            content = f"""✅ **Step 6: Compare Complete**

**Baseline:** `{baseline_name}`

**Results:**
├── AFTER CSV: {csv_pass} pass, {csv_fail} fail ✅
└── Root Mixed: {root_pass} pass, {root_fail} fail ✅

All files match! No differences found.

**Next:** Run `finalize {baseline_name}` (Step 8)"""
            
            await cl.Message(content=content).send()
        else:
            # Differences found - need approval
            failed_list = "\n".join([f"• {f}" for f in failed_files]) if failed_files else "• See report for details"
            
            content = f"""⚠️ **Step 6: Differences Found**

**Baseline:** `{baseline_name}`

**Results:**
├── AFTER CSV: {csv_pass} pass, {csv_fail} fail {"✅" if csv_fail == 0 else "⚠️"}
└── Root Mixed: {root_pass} pass, {root_fail} fail {"✅" if root_fail == 0 else "⚠️"}

**Differences in:**
{failed_list}

**Report:** `{report_path}`

Please review the report and confirm:"""
            
            # Create action buttons
            actions = [
                cl.Action(name="autonomous_fix", payload={"baseline": baseline_name, "failed_files": failed_files, "validate_mode": False}, label="🚀 Agent Auto-Fix"),
                cl.Action(name="analyse_compare_diff", payload={"baseline": baseline_name, "failed_files": failed_files, "validate_mode": False}, label="🤖 Analyse & Fix with AI"),
                cl.Action(name="compare_acceptable", payload={"baseline": baseline_name}, label="✅ Acceptable"),
                cl.Action(name="compare_not_acceptable", payload={"baseline": baseline_name}, label="❌ Not Acceptable")
            ]
            
            await cl.Message(content=content, actions=actions).send()
            
    except Exception as e:
        await cl.Message(content=f"❌ **Step 6: Unexpected Error**\n\n```\n{str(e)}\n```").send()
        logger.error(f"Unexpected error in handle_compare: {e}")


@cl.action_callback("compare_acceptable")
async def on_compare_acceptable(action: cl.Action):
    """Handle acceptable button click."""
    _ensure_project_root_on_path()
    from prompts.skills.compare import mark_acceptable
    baseline_name = action.payload.get("baseline")
    session = session_manager.get_session(baseline_name)
    
    success, message = mark_acceptable(session)
    
    await cl.Message(content=f"""✅ **Step 6: Complete**

Differences marked as **acceptable**.

**Next:** Run `finalize {baseline_name}` (Step 8)""").send()


@cl.action_callback("compare_not_acceptable")
async def on_compare_not_acceptable(action: cl.Action):
    """Handle not acceptable button click."""
    _ensure_project_root_on_path()
    from prompts.skills.compare import mark_not_acceptable
    baseline_name = action.payload.get("baseline")
    session = session_manager.get_session(baseline_name)
    
    success, message = mark_not_acceptable(session)
    
    await cl.Message(content=f"""❌ **Step 6: Not Acceptable**

Differences marked as **not acceptable**.

**Action needed:**
1. Review the differences in report
2. Fix the code
3. Run `run {baseline_name}` to re-test
4. Run `compare {baseline_name}` again

_💡 Tip: click **🤖 Analyse & Fix with AI** on the differences dialog to let the agent diagnose each mismatching file and loop fix → rerun → compare automatically._""").send()


def _edits_payload(suggestion) -> list:
    """Serialize a suggestion's edit hunks for an Action payload.

    Each entry is ``[original_code, suggested_code]``; carries every
    edit so the Auto Apply button can apply multi-location fixes."""
    return [[e.original_code, e.suggested_code] for e in suggestion.get_edits()]


@cl.action_callback("analyse_with_ai")
async def on_analyse_with_ai(action: cl.Action):
    baseline = action.payload.get("baseline")
    program_name = action.payload.get("program_name")
    error_message = action.payload.get("error_message", "Unknown error")
    error_type = action.payload.get("error_type", "unknown")
    stack_trace = action.payload.get("stack_trace")
    exit_code = action.payload.get("exit_code", 1)
    # ``validate_mode`` — carried forward when this button is clicked
    # from inside the validate composite. Threaded through every
    # follow-up action so ``on_mark_fix_applied`` can auto-continue
    # the composite (unload → compare → …) after a successful retry.
    validate_mode = bool(action.payload.get("validate_mode", False))

    await cl.Message(content=f"""🤖 **AI Analysis Started**

**Program:** `{program_name}`
**Baseline:** `{baseline}`

⏳ Analyzing error and searching for similar fixes...
This may take 30-60 seconds.""").send()

    try:
        analysis_agent = get_analysis_agent()

        suggestion = await analysis_agent.analyze_runtime_error(
            baseline_name=baseline,
            program_name=program_name,
            error_message=error_message,
            exit_code=exit_code,
            stack_trace=stack_trace
        )

        if suggestion.confidence >= 70:
            confidence_emoji = "🟢"
            confidence_text = "High"
        elif suggestion.confidence >= 50:
            confidence_emoji = "🟡"
            confidence_text = "Medium"
        else:
            confidence_emoji = "🔴"
            confidence_text = "Low"
        
        similar_fixes_text = ""
        if suggestion.similar_fixes_used:
            similar_fixes_text = "\n**📚 Similar Past Fixes Found:**\n"
            for fix in suggestion.similar_fixes_used[:3]:
                similar_fixes_text += f"• PR #{fix.pr_id}: `{fix.baseline}`\n"

        code_section = ""
        if suggestion.original_code and suggestion.suggested_code:
            code_section = (
                "\n---\n\n"
                "**❌ Original Code:**\n"
                "```csharp\n"
                + suggestion.original_code +
                "\n```\n\n"
                "**✅ Suggested Fix:**\n"
                "```csharp\n"
                + suggestion.suggested_code +
                "\n```\n"
                + suggestion.extra_edits_markdown()
            )

        review_warning = ""
        if suggestion.requires_manual_review:
            review_warning = "\n⚠️ **Manual review recommended** (confidence below 70%)\n"

        line_info = ""
        if suggestion.line_number:
            line_info = f"\n**Line Number:** {suggestion.line_number}"
    
        result_message = f"""🤖 **AI Analysis Complete**

**Program:** `{program_name}`
**Baseline:** `{baseline}`
**Confidence:** {confidence_emoji} {suggestion.confidence}% ({confidence_text})
{review_warning}
---

**🔍 Root Cause:**
{suggestion.root_cause}

---

**📋 Analysis:**
{suggestion.explanation}

---

**📁 File to Fix:** `{suggestion.file_to_fix}`{line_info}
{code_section}
---

**🏷️ Fix Type:** `{suggestion.fix_type}`
{similar_fixes_text}
---

**📝 Additional Notes:**
{suggestion.additional_notes or "None"}

---

**Next Steps:**
1. Review the suggested fix above
2. Apply changes to: `{suggestion.file_to_fix}`
3. Run `run {baseline}` to test the fix
4. If still fails, click "Re-analyse" for another suggestion"""
        
        session = analysis_agent.get_session(baseline)
        attempt_info = ""
        if session:
            attempt_info = f" (Attempt {session.current_attempt}/{session.max_attempts})"

        actions = [
            cl.Action(
                name="retry_analysis",
                payload={
                    "baseline": baseline,
                    "program_name": program_name,
                    "error_message": error_message,
                    "stack_trace": stack_trace,
                    "validate_mode": validate_mode,
                },
                label=f"🔄 Re-analyse{attempt_info}"
            ),
            cl.Action(
                name="auto_apply_fix",
                payload={
                    "baseline": baseline,
                    "program_name": program_name,
                    "file_to_fix": suggestion.file_to_fix,
                    "original_code": suggestion.original_code or "",
                    "suggested_code": suggestion.suggested_code or "",
                    "edits": _edits_payload(suggestion),
                    "validate_mode": validate_mode,
                },
                label="✨ Auto Apply"
            ),
            cl.Action(
                name="mark_fix_applied",
                payload={
                    "baseline": baseline,
                    "program_name": program_name,
                    "file_to_fix": suggestion.file_to_fix,
                    "validate_mode": validate_mode,
                },
                label="✅ Fix Applied - Run Again"
            )
        ]

        await cl.Message(content=result_message, actions=actions).send()

    except Exception as e:
        logger.error(f"AI Analysis error: {e}")
        await cl.Message(content=f"""❌ **AI Analysis Failed**

**Error:** `{str(e)}`

**Possible Causes:**
• EnsoAI API not configured (check .env)
• COBOL source not found in knowledge base
• Network/API timeout

**Fallback Steps:**
1. Review the error manually
2. Check COBOL source for expected behavior
3. Compare with similar programs
4. Fix and run `run {baseline}` again

**Debug:**
• Check logs for details
• Verify ENSO_AI_API_URL and ENSO_AI_API_KEY in .env""").send()


@cl.action_callback("retry_analysis")
async def on_retry_analysis(action: cl.Action):
    baseline = action.payload.get("baseline")
    program_name = action.payload.get("program_name")
    error_message = action.payload.get("error_message", "Unknown error")
    stack_trace = action.payload.get("stack_trace")
    validate_mode = bool(action.payload.get("validate_mode", False))

    await cl.Message(content=f"""🔄 **Re-analysing...**

**Program:** `{program_name}`

⏳ Trying a different approach...""").send()

    try:
        analysis_agent = get_analysis_agent()
        session = analysis_agent.get_session(baseline)

        if session and not session.can_retry:
            await cl.Message(content=f"""⚠️ **Maximum Attempts Reached**

**Program:** `{program_name}`
**Attempts:** {session.current_attempt}/{session.max_attempts}

The AI has tried {session.max_attempts} different approaches without success.

**Recommendation:**
• Review all previous suggestions
• Manual investigation required
• Consider creating a bug ticket

**Previous Attempts Summary:**
{session.get_failed_fixes_summary()}""").send()
            return

        suggestion = await analysis_agent.retry_analysis(
            baseline_name=baseline,
            new_error_message=error_message,
            stack_trace=stack_trace
        )
        
        if suggestion.confidence >= 70:
            confidence_emoji = "🟢"
        elif suggestion.confidence >= 50:
            confidence_emoji = "🟡"
        else:
            confidence_emoji = "🔴"

        session = analysis_agent.get_session(baseline)
        attempt_text = f"Attempt {session.current_attempt}/{session.max_attempts}" if session else ""

        code_section = ""
        if suggestion.original_code and suggestion.suggested_code:
            code_section = (
                "\n**❌ Original Code:**\n"
                "```csharp\n"
                + suggestion.original_code +
                "\n```\n\n"
                "**✅ Suggested Fix:**\n"
                "```csharp\n"
                + suggestion.suggested_code +
                "\n```\n"
                + suggestion.extra_edits_markdown()
            )

        result_message = f"""🤖 **Re-Analysis Complete** ({attempt_text})

**Program:** `{program_name}`
**Confidence:** {confidence_emoji} {suggestion.confidence}%

---

**🔍 Root Cause:**
{suggestion.root_cause}

---

**📁 File to Fix:** `{suggestion.file_to_fix}`
{code_section}
---

**📝 Notes:**
{suggestion.additional_notes or "None"}

---

**Next Steps:**
1. Apply this fix to: `{suggestion.file_to_fix}`
2. Run `run {baseline}` to test"""
        
        actions = []

        if session and session.can_retry:
            actions.append(
                cl.Action(
                    name="retry_analysis",
                    payload={
                        "baseline": baseline,
                        "program_name": program_name,
                        "error_message": error_message,
                        "stack_trace": stack_trace,
                        "validate_mode": validate_mode,
                    },
                    label=f"🔄 Try Again ({session.current_attempt}/{session.max_attempts})"
                )
            )

        actions.append(
            cl.Action(
                name="auto_apply_fix",
                payload={
                    "baseline": baseline,
                    "program_name": program_name,
                    "file_to_fix": suggestion.file_to_fix,
                    "original_code": suggestion.original_code or "",
                    "suggested_code": suggestion.suggested_code or "",
                    "edits": _edits_payload(suggestion),
                    "validate_mode": validate_mode,
                },
                label="✨ Auto Apply"
            )
        )

        actions.append(
            cl.Action(
                name="mark_fix_applied",
                payload={
                    "baseline": baseline,
                    "program_name": program_name,
                    "file_to_fix": suggestion.file_to_fix,
                    "validate_mode": validate_mode,
                },
                label="✅ Fix Applied - Run Again"
            )
        )

        await cl.Message(content=result_message, actions=actions).send()

    except ValueError as e:
        await cl.Message(content=f"""⚠️ **No Previous Analysis Found**

Run `run {baseline}` first to trigger an error, then click "Analyse with AI".""").send()

    except Exception as e:
        logger.error(f"Re-analysis error: {e}")
        await cl.Message(content=f"""❌ **Re-analysis Failed**

**Error:** `{str(e)}`

**Try:**
• Run `run {baseline}` again to get fresh error
• Then click "Analyse with AI" """).send()


@cl.action_callback("auto_apply_fix")
async def on_auto_apply_fix(action: cl.Action):
    """Automatically replace the buggy snippet with the AI's suggested
    fix in the target file. Additive to :func:`on_mark_fix_applied` —
    the user still clicks Run separately after apply."""
    baseline = action.payload.get("baseline")
    program_name = action.payload.get("program_name")
    file_to_fix = action.payload.get("file_to_fix", "")
    original_code = action.payload.get("original_code", "")
    suggested_code = action.payload.get("suggested_code", "")
    # ``edits`` carries every hunk (declaration + reset, etc.); fall back
    # to the single original/suggested pair for older payloads.
    edits_payload = action.payload.get("edits") or []
    compare_mode = bool(action.payload.get("compare_mode", False))

    if not file_to_fix or not original_code or not suggested_code:
        await cl.Message(content=(
            "⚠️ **Auto Apply skipped**\n\n"
            "Missing target file or code snippet in the AI suggestion. "
            "Please apply manually and then click **✅ Fix Applied - Run Again**."
        )).send()
        return

    if edits_payload:
        result = apply_fixes_to_file(
            file_path=file_to_fix,
            edits=[(e[0], e[1]) for e in edits_payload],
        )
    else:
        result = apply_fix_to_file(
            file_path=file_to_fix,
            original_code=original_code,
            suggested_code=suggested_code,
        )

    if not result.ok:
        await cl.Message(content=(
            f"⚠️ **Auto Apply could not modify the file**\n\n"
            f"**Reason:** {result.message}\n\n"
            f"**File:** `{file_to_fix}`\n\n"
            f"Please copy the suggested code above and paste it into the "
            f"file manually, then click **✅ Fix Applied - Run Again**."
        )).send()
        return

    line_note = f" at **line {result.line_start}**" if result.line_start else ""

    # ArcEdit only refreshes .xrf when the .cs edit goes through Visual Studio;
    # Auto Apply writes the file directly, so we invoke the XRF sync script.
    xrf_result = await asyncio.to_thread(update_xrf_from_cs_edit, file_to_fix)
    if xrf_result.ok:
        xrf_status = "✅ XRF file updated."
    elif xrf_result.skipped:
        xrf_status = f"ℹ️ XRF update skipped: {xrf_result.message}"
    else:
        xrf_status = (
            f"⚠️ Code was updated, but XRF update failed: {xrf_result.message}"
        )

    next_step_hint = (
        "**Next Step:** Click **✅ Fix Applied — Rerun & Compare** to re-run "
        "the program and re-compare automatically."
        if compare_mode else
        f"**Next Step:** Click **✅ Fix Applied - Run Again** to test the fix "
        f"(or run `run {baseline}` from the prompt)."
    )

    await cl.Message(content=(
        f"✨ **Auto Apply Complete**\n\n"
        f"Replaced the buggy snippet{line_note} in `{file_to_fix}`.\n\n"
        f"{xrf_status}\n\n"
        f"{next_step_hint}"
    )).send()


# ═════════════════════════════════════════════════════════════════════
# /add-issue — curated Issues KB single-template form
# The user pastes ONE completed template block; the server parses it,
# shows a preview, and (on Save) writes to the ``issues`` collection.
# Session state ``add_issue_state`` = {"awaiting_paste": True} while
# the app is waiting for the user's filled-in template.
# ═════════════════════════════════════════════════════════════════════

_ADD_ISSUE_TEMPLATE = (
    "```yaml\n"
    "title:            \n"
    "program:          \n"
    "error_signature:  \n"
    "bug_category:     other\n"
    "tags:             \n"
    "```\n"
    "\n"
    "===== SYMPTOMS =====\n"
    "\n"
    "===== ROOT CAUSE =====\n"
    "\n"
    "===== BUGGY CODE =====\n"
    "```csharp\n"
    "// paste the buggy C# snippet here\n"
    "```\n"
    "\n"
    "===== FIXED CODE =====\n"
    "```csharp\n"
    "// paste the correct C# snippet here\n"
    "```\n"
)


async def _add_issue_wizard_start():
    """Send the fill-in template and wait for the user's paste."""
    cl.user_session.set("add_issue_state", {"awaiting_paste": True})
    intro = (
        "✏️ **Add issue to KB**\n\n"
        "Copy the template below, fill in the values, and paste the completed "
        "block back as a single message. Type `cancel` to abort.\n\n"
        "**Category options:** `wrapper_value_mismatch`, `wrong_variable_name`, "
        "`extra_function_wrapper`, `missing_container_prefix`, `format_conversion`, "
        "`file_status_check`, `other`.\n\n"
        "**Optional fields** (leave blank if not applicable): `error_signature`, "
        "`root_cause`.\n\n"
        "──────────── TEMPLATE ────────────\n"
        f"{_ADD_ISSUE_TEMPLATE}"
        "──────────────────────────────────"
    )
    await cl.Message(content=intro).send()


def _parse_add_issue_template(text: str) -> Tuple[dict, list]:
    """Parse the user's filled-in template.

    Returns ``(entry_dict, errors)``. ``errors`` is a list of
    human-readable strings; if non-empty the entry is invalid.
    """
    import re as _re

    errors: List[str] = []
    entry: dict = {}

    # Single-line YAML fields. ``root_cause`` is kept here as a
    # backward-compat fallback for pre-multi-line-block templates; the
    # newer ===== ROOT CAUSE ===== block (if present) overrides it.
    fields = ("title", "program", "error_signature", "bug_category", "tags", "root_cause")
    for f in fields:
        m = _re.search(
            rf"^[ \t]*{f}[ \t]*:[ \t]*(.*)$",
            text, _re.MULTILINE | _re.IGNORECASE,
        )
        entry[f] = m.group(1).strip() if m else ""

    def _extract_between(start_marker: str, end_marker: Optional[str]) -> str:
        pattern = rf"{_re.escape(start_marker)}\s*(?:```[^\n]*\n)?(.*?)(?:```)?\s*"
        pattern += _re.escape(end_marker) if end_marker else r"\Z"
        m = _re.search(pattern, text, _re.DOTALL | _re.IGNORECASE)
        return m.group(1).strip() if m else ""

    # Multi-line blocks. Order in the template (earliest to latest end
    # marker) drives the ``end_marker`` argument here.
    symptoms = _extract_between(
        "===== SYMPTOMS =====", "===== ROOT CAUSE ====="
    )
    if not symptoms:
        # ROOT CAUSE block may be absent — fall through to BUGGY CODE.
        symptoms = _extract_between(
            "===== SYMPTOMS =====", "===== BUGGY CODE ====="
        )
    root_cause = _extract_between(
        "===== ROOT CAUSE =====", "===== BUGGY CODE ====="
    )
    buggy = _extract_between("===== BUGGY CODE =====", "===== FIXED CODE =====")
    fixed = _extract_between("===== FIXED CODE =====", None)

    # Multi-line block overrides the YAML fallback when populated.
    if root_cause:
        entry["root_cause"] = root_cause

    for label, block in (
        ("symptoms", symptoms),
        ("buggy_code", buggy),
        ("fixed_code", fixed),
    ):
        cleaned = block
        cleaned = _re.sub(r"^```[^\n]*\n", "", cleaned)
        cleaned = _re.sub(r"\n```\s*$", "", cleaned)
        cleaned = cleaned.strip()
        entry[label] = cleaned

    if not entry.get("title"):
        errors.append("`title` is required")
    if not entry.get("buggy_code") or entry["buggy_code"].startswith("//"):
        errors.append("`BUGGY CODE` block is empty or still contains the placeholder comment")
    if not entry.get("fixed_code") or entry["fixed_code"].startswith("//"):
        errors.append("`FIXED CODE` block is empty or still contains the placeholder comment")
    if not entry.get("bug_category"):
        entry["bug_category"] = "other"

    return entry, errors


async def _add_issue_wizard_step(user_input: str):
    """Process the pasted template. On success show preview + actions."""
    stripped = user_input.strip()
    if stripped.lower() == "cancel":
        cl.user_session.set("add_issue_state", None)
        await cl.Message(content="❌ Cancelled. No issue was saved.").send()
        return

    entry, errors = _parse_add_issue_template(user_input)
    if errors:
        bullet = "\n".join(f"• {e}" for e in errors)
        await cl.Message(content=(
            f"⚠️ **Template incomplete:**\n{bullet}\n\n"
            "Fix the issues above and paste the corrected template again, "
            "or type `cancel` to abort."
        )).send()
        return

    cl.user_session.set("add_issue_state", {"awaiting_paste": False, "entry": entry})
    await _add_issue_wizard_preview(entry)


async def _add_issue_wizard_preview(entry: dict):
    """Show a preview and offer Save / Cancel actions."""
    tags_raw = entry.get("tags", "")
    tags_list = [t.strip() for t in tags_raw.split(",") if t.strip()]

    err_display = entry.get("error_signature", "")
    if not err_display or err_display.lower() == "none":
        err_display = "_(none — output-diff bug or not applicable)_"

    preview = (
        "🔎 **Preview**\n\n"
        f"**Title:**       {entry.get('title', '')}\n"
        f"**Program:**     {entry.get('program', '') or '_(any)_'}\n"
        f"**Category:**    `{entry.get('bug_category', 'other')}`\n"
        f"**Tags:**        {', '.join(tags_list) or '_(none)_'}\n"
        f"**Error:**       {err_display}\n\n"
        f"**Symptoms:**\n{entry.get('symptoms') or '_(not provided)_'}\n\n"
        f"**Root cause:**\n{entry.get('root_cause') or '_(not provided)_'}\n\n"
        "**Buggy code:**\n"
        f"```csharp\n{entry.get('buggy_code', '')}\n```\n"
        "**Fixed code:**\n"
        f"```csharp\n{entry.get('fixed_code', '')}\n```"
    )

    actions = [
        cl.Action(
            name="add_issue_save",
            payload={},
            label="✅ Save to KB",
        ),
        cl.Action(
            name="add_issue_cancel",
            payload={},
            label="❌ Cancel",
        ),
    ]
    await cl.Message(content=preview, actions=actions).send()


def _generate_issue_id(kb) -> str:
    """Return next unused ``ISS-YYYYMMDD-NNN`` id."""
    from datetime import datetime
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"ISS-{today}-"
    for n in range(1, 1000):
        candidate = f"{prefix}{n:03d}"
        if not kb.issue_exists(candidate):
            return candidate
    return f"ISS-{today}-{int(datetime.now().timestamp())}"


@cl.action_callback("add_issue_save")
async def on_add_issue_save(action: cl.Action):
    """Persist the wizard-collected entry to the ``issues`` collection."""
    from datetime import datetime
    from services.knowledge_base_service import get_kb_service

    state = cl.user_session.get("add_issue_state") or {}
    entry = state.get("entry")
    if not entry:
        await cl.Message(content="⚠️ No pending entry to save. Type `/add-issue` to start.").send()
        return

    kb = get_kb_service()
    issue_id = _generate_issue_id(kb)

    tags = [t.strip() for t in entry.get("tags", "").split(",") if t.strip()]
    error_sig = entry.get("error_signature", "")
    if error_sig.lower() == "none":
        error_sig = ""

    ok = kb.add_issue(
        issue_id=issue_id,
        title=entry.get("title", ""),
        buggy_code=entry.get("buggy_code", ""),
        fixed_code=entry.get("fixed_code", ""),
        bug_category=entry.get("bug_category", "other") or "other",
        tags=tags,
        program=entry.get("program", ""),
        error_signature=error_sig,
        symptoms=entry.get("symptoms", ""),
        root_cause=entry.get("root_cause", ""),
        source="manual-chat",
        resolved_date=datetime.now().strftime("%Y-%m-%d"),
        resolved_by=cl.user_session.get("user_id") or "",
        verified=False,
    )

    cl.user_session.set("add_issue_state", None)

    if ok:
        total = kb.count_issues()
        await cl.Message(content=(
            f"✅ **Saved as `{issue_id}`**\n\n"
            f"Issues KB now has **{total}** entr{'y' if total == 1 else 'ies'}. "
            "This pattern will be retrieved when similar bugs are analysed."
        )).send()
    else:
        await cl.Message(content=(
            "❌ **Save failed.** Check the app logs for details. "
            "Your wizard state was cleared — start again with `/add-issue`."
        )).send()


@cl.action_callback("add_issue_cancel")
async def on_add_issue_cancel(action: cl.Action):
    """Discard the in-progress wizard entry."""
    cl.user_session.set("add_issue_state", None)
    await cl.Message(content="❌ Cancelled. No issue was saved.").send()


@cl.action_callback("mark_fix_applied")
async def on_mark_fix_applied(action: cl.Action):
    baseline = action.payload.get("baseline")
    program_name = action.payload.get("program_name")
    file_to_fix = action.payload.get("file_to_fix", "")
    validate_mode = bool(action.payload.get("validate_mode", False))
    compare_mode = bool(action.payload.get("compare_mode", False))
    await cl.Message(content=f"""✅ **Fix Applied**

**File:** `{file_to_fix}`

Now re-running the program to test...""").send()

    if validate_mode:
        # Composite path — retry Run step and, on success, auto-continue
        # the validate workflow from step 5 (unload) onwards.
        await _retry_run_inside_validate(baseline)
    elif compare_mode:
        # Standalone compare auto-fix loop: run → compare → decide.
        if not program_name:
            program_name = _extract_program_name_safe(baseline)
        await _run_then_compare_standalone(baseline, program_name)
    else:
        # Existing standalone path — unchanged behaviour.
        await handle_run(baseline)




async def handle_report(baseline: str):
    if not baseline:
        await cl.Message(content=_msg("usage.report")).send()
        return
    
    await cl.Message(content=_msg("not_implemented.report", baseline=baseline)).send()


async def handle_finalize(baseline: str):
    """Step 8: commit, push, PR, close validation ticket. UI wrapper."""
    if not baseline:
        await cl.Message(content=_msg("usage.finalize")).send()
        return

    session = session_manager.get_session(baseline)
    orchestrator = get_orchestrator()

    loop = asyncio.get_event_loop()
    future = loop.run_in_executor(
        None,
        lambda: asyncio.run(orchestrator.run_skill("finalize", session)),
    )

    async with ProgressCard(
        "Step 8: Finalize & Deliver", baseline, interval=10, eta_seconds=90
    ) as card:
        await card.wait_for(future, status="Committing, pushing, PR, ticket close...")
        success, result = await future
        if success:
            await card.complete("Finalize complete")
        else:
            await card.fail(result.get("message") or "Finalize failed")

    if success:
        await cl.Message(content=_msg(
            "finalize.success",
            baseline=baseline,
            branch=result.get("branch_name") or "",
            pr_id=result.get("pr_id") or "",
            pr_url=result.get("pr_url") or "",
            ticket_id=result.get("ticket_id") or "",
        )).send()
    else:
        await cl.Message(content=_msg(
            "finalize.failure",
            baseline=baseline,
            error=result.get("message") or "Unknown error",
        )).send()


async def handle_validate(baseline_name: str):
    """Handle the `validate <baseline>` command — full 7-step composite.

    Pure UI layer. Step sequencing, branching, and both finalize
    stages live in the composite agent at
    ``agents.validation_agent.validate_agent``. This function
    consumes the events that agent yields and renders them into
    Chainlit UI using templates from ``prompts/VALIDATE_SKILL.md``.
    """
    if not baseline_name:
        await cl.Message(content=_msg("usage.validate")).send()
        return

    from agents.validation_agent.validate_agent import (
        run as run_validate,
        STEPS as VALIDATE_STEPS,
        FINALIZE_STEP_NUM,
    )

    skill = load_validate_skill()
    session = session_manager.get_session(baseline_name)
    total = skill.get("total_steps", 7)

    start_msg = (skill.get("start_message") or "").strip()
    if start_msg:
        await cl.Message(content=start_msg.format(baseline_name=baseline_name)).send()

    # Pipeline HUD — 7 titles (6 sub-agents + 1 combined Finalize row)
    step_titles = ", ".join([t for _, _, t in VALIDATE_STEPS] + ["Finalize"])
    step_ops    = ", ".join([o for _, o, _ in VALIDATE_STEPS] + ["finalize"])
    await _emit_pipeline_event(
        "start",
        baseline=baseline_name,
        steps=step_titles,
        ops=step_ops,
    )

    await _consume_validate_events(
        run_validate(session), baseline_name, skill, total,
    )


# ═════════════════════════════════════════════════════════════════════
# Validate composite — consumer + retry helper + 4 action callbacks
# ═════════════════════════════════════════════════════════════════════
# Consumer is separate from _consume_reporting_events (per user
# decision: safer, no risk to the proven reporting flow).
# All UI strings come from prompts/VALIDATE_SKILL.md.


async def _consume_validate_events(
    event_stream,
    baseline_name: str,
    skill: dict,
    total: int,
    initial_report_path: str = "",
):
    """Render Chainlit UI in response to events from validate_agent.

    Handles all 8 event phases:
      step, run_fail, all_pass, differences,
      await_push, push_done, await_pr, finalize_done
    """
    from agents.validation_agent.validate_agent import FINALIZE_STEP_NUM

    steps_by_op = {s["id"]: s for s in skill.get("steps", [])}
    cmp_cfg           = skill.get("compare", {})
    run_fail_cfg      = skill.get("run_fail", {})
    await_push_cfg    = skill.get("await_push", {})
    finalize_push_cfg = skill.get("finalize_push", {})
    await_pr_cfg      = skill.get("await_pr", {})
    finalize_pr_cfg   = skill.get("finalize_pr", {})
    report_path       = initial_report_path

    async for event in event_stream:
        phase = event["phase"]

        # ── step ────────────────────────────────────────────────
        if phase == "step":
            num   = event["num"]
            op    = event["op"]
            title = event["title"]
            task  = event["task"]

            await _emit_pipeline_event("step_start", num=num, op=op)

            if op == "compare":
                cfg = cmp_cfg
            elif op == "finalize_push":
                cfg = finalize_push_cfg
            elif op == "finalize_pr":
                cfg = finalize_pr_cfg
            else:
                cfg = steps_by_op.get(op, {})

            async with ProgressCard(
                f"Step {num}/{total}: {title}", baseline_name,
                interval=cfg.get("progress_interval", 10),
                eta_seconds=cfg.get("progress_eta", 120),
            ) as card:
                await card.wait_for(
                    task, status=cfg.get("progress_status", "Processing..."),
                )
                ok, result = await task
                if isinstance(result, str):
                    result = {"message": result}
                if ok:
                    await card.complete(
                        cfg.get("progress_complete", f"{title} complete")
                    )
                else:
                    error = _extract_step_error(result, cfg.get("error_path"))
                    await card.fail(
                        error or cfg.get("progress_fail_default", "Failed")
                    )

            if not ok:
                # Run failure is handled by a dedicated `run_fail` event
                # yielded right after this one — skip generic rendering.
                if op == "run":
                    continue
                # Compare/other failures use the generic fail template.
                fail_tpl = cfg.get("fail", "❌ Step failed: {error}")
                error = (
                    _extract_step_error(result, cfg.get("error_path"))
                    or "Unknown error"
                )
                safe_kwargs = {
                    "error": error,
                    "baseline_name": baseline_name,
                    "report_path": report_path or "N/A",
                    "branch_name": (
                        result.get("branch_name") if isinstance(result, dict) else ""
                    ) or "",
                }
                try:
                    await cl.Message(content=fail_tpl.format(**safe_kwargs)).send()
                except (KeyError, IndexError, ValueError):
                    await cl.Message(content=fail_tpl).send()
                await _emit_pipeline_event("step_fail", num=num, op=op)
                return

            # Success path
            if op == "compare":
                report_path = result.get("report_path", "") or report_path
                continue
            if op in ("finalize_push", "finalize_pr"):
                # Rendered by push_done / finalize_done events.
                continue

            await _emit_pipeline_event("step_done", num=num, op=op)
            success_tpl = cfg.get("success", "")
            if success_tpl:
                try:
                    safe = {
                        k: (v if v is not None else "")
                        for k, v in result.items()
                        if isinstance(k, str)
                    }
                    await cl.Message(content=success_tpl.format(**safe)).send()
                except (KeyError, IndexError, ValueError):
                    await cl.Message(content=success_tpl).send()

        # ── run_fail ────────────────────────────────────────────
        elif phase == "run_fail":
            num = event["num"]
            result = event.get("result") or {}
            error_info = result.get("error_info") or {}
            error_type = error_info.get("error_type", "unknown")
            error_msg = error_info.get("error_message", "Unknown error")
            stack_trace = error_info.get("stack_trace", "")
            sql_error = error_info.get("sql_error", "")
            exit_code = result.get("exit_code", 1)
            program_name = (
                result.get("program_name")
                or _extract_program_name_safe(baseline_name)
            )

            await _emit_pipeline_event("step_fail", num=num, op="run")

            # Reuse the exact error-display format from the standalone
            # handle_run so the UX is identical.
            error_display = f"**Error Type:** `{error_type}`\n\n"
            if sql_error:
                error_display += f"**SQL Error:**\n```\n{sql_error}\n```\n\n"
            if error_msg:
                error_display += f"**Message:**\n```\n{error_msg}\n```\n\n"
            if stack_trace:
                error_display += f"**Stack Trace:**\n```\n{stack_trace}\n```\n\n"

            header = (run_fail_cfg.get("header") or "").format(
                program_name=program_name, baseline_name=baseline_name,
            )
            guidance = (run_fail_cfg.get("guidance") or "").strip()
            content = f"{header}\n{error_display}\n{guidance}"

            # Key: validate_mode=True → on_mark_fix_applied resumes the
            # composite instead of showing manual next-steps.
            actions = [
                cl.Action(
                    name="analyse_with_ai",
                    payload={
                        "baseline": baseline_name,
                        "program_name": program_name,
                        "error_message": error_msg,
                        "error_type": error_type,
                        "stack_trace": stack_trace,
                        "exit_code": exit_code,
                        "validate_mode": True,
                    },
                    label="🤖 Analyse with AI",
                )
            ]
            await cl.Message(content=content, actions=actions).send()
            return

        # ── all_pass ────────────────────────────────────────────
        elif phase == "all_pass":
            result = event["result"]
            await _emit_pipeline_event("step_done", num=6, op="compare")
            all_pass_tpl = cmp_cfg.get(
                "all_pass", "✅ **Step 6: Compare - All Pass!**"
            )
            await cl.Message(content=all_pass_tpl.format(
                csv_pass=result.get("after_csv_pass", 0),
                csv_fail=result.get("after_csv_fail", 0),
                root_pass=result.get("root_mixed_pass", 0),
                root_fail=result.get("root_mixed_fail", 0),
            ).strip()).send()

        # ── differences ─────────────────────────────────────────
        elif phase == "differences":
            result = event["result"]
            report_path = result.get("report_path", "")
            await _emit_pipeline_event("step_pause", num=6, op="compare")
            csv_pass = result.get("after_csv_pass", 0)
            csv_fail = result.get("after_csv_fail", 0)
            root_pass = result.get("root_mixed_pass", 0)
            root_fail = result.get("root_mixed_fail", 0)
            failed_files = result.get("failed_files", [])
            failed_list = (
                "\n".join([f"• {f}" for f in failed_files])
                if failed_files else "• See report"
            )
            csv_icon  = "✅" if csv_fail == 0 else "⚠️"
            root_icon = "✅" if root_fail == 0 else "⚠️"

            diff_tpl = cmp_cfg.get(
                "differences_found", "⚠️ **Step 6: Differences Found**"
            )
            content = diff_tpl.format(
                csv_pass=csv_pass, csv_fail=csv_fail,
                root_pass=root_pass, root_fail=root_fail,
                csv_icon=csv_icon, root_icon=root_icon,
                failed_list=failed_list, report_path=report_path,
            ).strip()

            actions = [
                cl.Action(
                    name="autonomous_fix",
                    payload={
                        "baseline": baseline_name,
                        "failed_files": failed_files,
                        "validate_mode": True,
                    },
                    label="🚀 Agent Auto-Fix",
                ),
                cl.Action(
                    name="analyse_compare_diff",
                    payload={
                        "baseline": baseline_name,
                        "failed_files": failed_files,
                        "validate_mode": True,
                    },
                    label="🤖 Analyse & Fix with AI",
                ),
                cl.Action(
                    name="validate_acceptable",
                    payload={"baseline": baseline_name, "report_path": report_path},
                    label=cmp_cfg.get("acceptable_label", "✅ Acceptable"),
                ),
                cl.Action(
                    name="validate_not_acceptable",
                    payload={"baseline": baseline_name, "report_path": report_path},
                    label=cmp_cfg.get(
                        "not_acceptable_label", "❌ Not Acceptable",
                    ),
                ),
            ]
            await cl.Message(content=content, actions=actions).send()
            return

        # ── await_push ──────────────────────────────────────────
        elif phase == "await_push":
            content = (await_push_cfg.get("message") or "").format(
                baseline_name=baseline_name,
                report_path=report_path or "N/A",
            ).strip()
            actions = [
                cl.Action(
                    name="validate_approve_push",
                    payload={
                        "baseline": baseline_name,
                        "report_path": report_path,
                    },
                    label=await_push_cfg.get(
                        "button_label", "🚀 Proceed to Push",
                    ),
                ),
            ]
            await cl.Message(content=content, actions=actions).send()
            return

        # ── push_done ───────────────────────────────────────────
        elif phase == "push_done":
            ok = event["ok"]
            result = event.get("result") or {}
            if ok:
                success_tpl = finalize_push_cfg.get("success", "")
                if success_tpl:
                    safe = {
                        "message": result.get("message", ""),
                        "branch_name": result.get("branch_name", ""),
                        "baseline_name": baseline_name,
                    }
                    try:
                        await cl.Message(content=success_tpl.format(**safe)).send()
                    except (KeyError, IndexError, ValueError):
                        await cl.Message(content=success_tpl).send()
            else:
                await _emit_pipeline_event(
                    "step_fail", num=FINALIZE_STEP_NUM, op="finalize",
                )
                fail_tpl = finalize_push_cfg.get("fail", "❌ Push failed: {error}")
                try:
                    await cl.Message(content=fail_tpl.format(
                        error=result.get("message", "Unknown error"),
                        baseline_name=baseline_name,
                        branch_name=result.get("branch_name", ""),
                    )).send()
                except (KeyError, IndexError, ValueError):
                    await cl.Message(content=fail_tpl).send()

        # ── await_pr ────────────────────────────────────────────
        elif phase == "await_pr":
            result = event.get("result") or {}
            content = (await_pr_cfg.get("message") or "").format(
                baseline_name=baseline_name,
                branch_name=result.get("branch_name", ""),
            ).strip()
            actions = [
                cl.Action(
                    name="validate_approve_pr",
                    payload={
                        "baseline": baseline_name,
                        "branch_name": result.get("branch_name", ""),
                    },
                    label=await_pr_cfg.get(
                        "button_label", "📥 Raise PR & Close Ticket",
                    ),
                ),
            ]
            await cl.Message(content=content, actions=actions).send()
            return

        # ── finalize_done ───────────────────────────────────────
        elif phase == "finalize_done":
            ok = event["ok"]
            result = event.get("result") or {}
            logger.info(
                f"[validate] finalize_done reached: ok={ok}, "
                f"pr_id={result.get('pr_id')}, "
                f"ticket_id={result.get('ticket_id')}, "
                f"ticket_closed={result.get('ticket_closed')}"
            )

            # ALWAYS update the pipeline HUD FIRST so the step turns
            # green/red even if the message rendering below stumbles.
            try:
                await _emit_pipeline_event(
                    "step_done" if ok else "step_fail",
                    num=FINALIZE_STEP_NUM, op="finalize",
                )
                if ok:
                    await _emit_pipeline_event("complete")
            except Exception as hud_exc:
                logger.warning(f"[validate] pipeline HUD emit failed: {hud_exc}")

            # Now render the success / failure message. Any error here
            # is logged but does NOT re-raise — the workflow has
            # already succeeded from a data perspective.
            try:
                if ok:
                    org = config.AZURE_DEVOPS_ORG
                    project = config.AZURE_DEVOPS_PROJECT
                    ticket_id = result.get("ticket_id") or ""
                    ticket_url = (
                        f"https://dev.azure.com/{org}/{project}"
                        f"/_workitems/edit/{ticket_id}"
                    ) if ticket_id else ""

                    success_tpl = (finalize_pr_cfg.get("success") or "").strip()
                    if success_tpl:
                        safe = {
                            "baseline_name": baseline_name,
                            "branch_name":   str(result.get("branch_name") or ""),
                            "pr_id":         str(result.get("pr_id") or ""),
                            "pr_url":        str(result.get("pr_url") or ""),
                            "ticket_id":     str(ticket_id),
                            "ticket_url":    ticket_url,
                        }
                        try:
                            rendered = success_tpl.format(**safe)
                        except (KeyError, IndexError, ValueError) as fmt_exc:
                            logger.warning(
                                f"[validate] success template format failed "
                                f"({fmt_exc}); sending raw template."
                            )
                            rendered = success_tpl
                        await cl.Message(content=rendered).send()
                        logger.info(
                            "[validate] final success message sent for "
                            f"{baseline_name}"
                        )
                else:
                    fail_tpl = (finalize_pr_cfg.get("fail") or "").strip()
                    try:
                        rendered = fail_tpl.format(
                            error=str(result.get("message") or "Unknown error"),
                            baseline_name=baseline_name,
                            branch_name=str(result.get("branch_name") or ""),
                        )
                    except (KeyError, IndexError, ValueError):
                        rendered = fail_tpl
                    await cl.Message(content=rendered).send()
            except Exception as msg_exc:
                logger.error(
                    f"[validate] final message render failed for "
                    f"{baseline_name}: {msg_exc}"
                )
                # Last-ditch simple message so user knows the workflow
                # completed (or failed) at least.
                fallback = (
                    f"🎉 **Validation Complete!** — PR + ticket close done."
                    if ok
                    else f"⚠️ **Validation Finalize Failed** — see logs."
                )
                try:
                    await cl.Message(content=fallback).send()
                except Exception:
                    pass
    # end of consumer


def _extract_program_name_safe(baseline_name: str) -> str:
    """Best-effort program-name extraction — mirrors the run skill's
    ``extract_program_name`` but keeps this file independent."""
    try:
        _ensure_project_root_on_path()
        from prompts.skills.run import extract_program_name
        return extract_program_name(baseline_name) or ""
    except Exception:  # pragma: no cover
        parts = (baseline_name or "").split("-", 1)
        return parts[1] if len(parts) == 2 else (baseline_name or "")


async def _retry_run_inside_validate(baseline_name: str):
    """Retry the Run step (only) inside the validate composite.

    Called by ``on_mark_fix_applied`` when the action carries
    ``validate_mode=True``. On success, auto-continues the composite
    from Unload (step 5) via ``resume_after_run_success``. On
    failure, re-emits the same run_fail UI so the user can Analyse +
    fix again.
    """
    from agents.validation_agent.validate_agent import (
        resume_after_run_success, STEPS as VALIDATE_STEPS,
    )

    _ensure_project_root_on_path()
    from prompts.skills.run import extract_program_name, find_program_file
    program_name = extract_program_name(baseline_name)

    found, path_or_msg = find_program_file(program_name)
    if not found:
        await cl.Message(content=(
            f"❌ **Program Not Found**\n\n{path_or_msg}\n\n"
            f"💡 Create the program first, then click Fix Applied again."
        )).send()
        return

    session = session_manager.get_session(baseline_name)
    skill = load_validate_skill()

    # Re-open pipeline HUD (may have been dismissed by user)
    step_titles = ", ".join([t for _, _, t in VALIDATE_STEPS] + ["Finalize"])
    step_ops    = ", ".join([o for _, o, _ in VALIDATE_STEPS] + ["finalize"])
    await _emit_pipeline_event(
        "resume", baseline=baseline_name,
        steps=step_titles, ops=step_ops,
    )
    await _emit_pipeline_event("step_start", num=4, op="run")

    orchestrator = get_orchestrator()
    loop = asyncio.get_event_loop()
    future = loop.run_in_executor(
        None,
        lambda: asyncio.run(orchestrator.run_skill("run", session)),
    )

    async with ProgressCard(
        f"Step 4/7: Run {program_name} (retry)", baseline_name,
        interval=15, eta_seconds=180,
    ) as card:
        await card.wait_for(future, status=f"Re-executing `{path_or_msg}`...")
        ok, result = await future
        if ok:
            await card.complete("Run complete")
        else:
            error_info = (result or {}).get("error_info") or {}
            err = error_info.get("error_message", "Failed")
            await card.fail(err)

    if not ok:
        # Re-emit the same run_fail UI so the user can iterate
        await _emit_pipeline_event("step_fail", num=4, op="run")
        result = result or {}
        error_info = result.get("error_info") or {}
        error_type = error_info.get("error_type", "unknown")
        error_msg = error_info.get("error_message", "Unknown error")
        stack_trace = error_info.get("stack_trace", "")
        sql_error = error_info.get("sql_error", "")
        exit_code = result.get("exit_code", 1)

        error_display = f"**Error Type:** `{error_type}`\n\n"
        if sql_error:
            error_display += f"**SQL Error:**\n```\n{sql_error}\n```\n\n"
        if error_msg:
            error_display += f"**Message:**\n```\n{error_msg}\n```\n\n"
        if stack_trace:
            error_display += f"**Stack Trace:**\n```\n{stack_trace}\n```\n\n"

        run_fail_cfg = skill.get("run_fail", {})
        header = (run_fail_cfg.get("header") or "").format(
            program_name=program_name, baseline_name=baseline_name,
        )
        guidance = (run_fail_cfg.get("guidance") or "").strip()
        content = f"{header}\n{error_display}\n{guidance}"

        actions = [
            cl.Action(
                name="analyse_with_ai",
                payload={
                    "baseline": baseline_name,
                    "program_name": program_name,
                    "error_message": error_msg,
                    "error_type": error_type,
                    "stack_trace": stack_trace,
                    "exit_code": exit_code,
                    "validate_mode": True,
                },
                label="🤖 Analyse with AI",
            )
        ]
        await cl.Message(content=content, actions=actions).send()
        return

    # Success — mark HUD done, brief ack, continue composite from step 5
    await _emit_pipeline_event("step_done", num=4, op="run")
    await cl.Message(content=(
        "✅ **Run passed on retry!** Continuing with Unload → Compare → …"
    )).send()

    await _consume_validate_events(
        resume_after_run_success(session), baseline_name, skill,
        total=skill.get("total_steps", 7),
    )


# ═════════════════════════════════════════════════════════════════════
# AUTOPILOT — separate, opt-in, NL-driven Copilot-style fix agent
# ═════════════════════════════════════════════════════════════════════
# Enabled only when the user clicks the dashboard Autopilot button (sends
# "/autopilot") or while an Autopilot session is live. Reuses the SAME
# operations (compare/run/unload + AnalysisAgent + apply_fix) read-only;
# does NOT touch the compare auto-fix loop, the compare skill, diff_reader,
# analyze_output_diff or fix_suggestion.yaml. State is held separately in
# ``cl.user_session["autopilot"]`` so nothing else is affected.

_AUTOPILOT_MAX_FILE_BYTES = 8000
_AUTOPILOT_MAX_FILES = 3


def _autopilot_state() -> dict:
    return cl.user_session.get("autopilot") or {}


def _autopilot_save(state: dict) -> None:
    cl.user_session.set("autopilot", state)


def _autopilot_clear() -> None:
    cl.user_session.set("autopilot_active", False)
    cl.user_session.set("autopilot", None)


def _autopilot_extract_file_guidance(elements) -> str:
    """Read text-like uploaded files into a capped guidance string."""
    if not elements:
        return ""
    from pathlib import Path as _P
    parts: list = []
    for el in list(elements)[:_AUTOPILOT_MAX_FILES]:
        path = getattr(el, "path", None)
        name = getattr(el, "name", "") or "file"
        if not path:
            continue
        try:
            text = _P(path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if not text.strip():
            continue
        if len(text) > _AUTOPILOT_MAX_FILE_BYTES:
            text = text[:_AUTOPILOT_MAX_FILE_BYTES] + "\n… (truncated)"
        parts.append(f"### Attached file `{name}`\n{text}")
    return "\n\n".join(parts)


async def _ap_run(loop, orchestrator, op: str, session):
    """Run a blocking skill off the event loop; never raise."""
    try:
        return await loop.run_in_executor(
            None, lambda: asyncio.run(orchestrator.run_skill(op, session))
        )
    except Exception as exc:  # noqa: BLE001
        return False, {
            "message": str(exc),
            "error_info": {"error_message": str(exc)},
        }


async def _autopilot_ask(state: dict, question: str) -> None:
    """Pause the loop and ask the user; control returns on their next message."""
    state["phase"] = "await_user"
    _autopilot_save(state)
    await cl.Message(content=(
        f"🤔 **Autopilot needs your input**\n\n{question}\n\n"
        f"_Reply with a hint/explanation (or attach a file), or send "
        f"`/autopilot stop` to exit._"
    )).send()


async def handle_autopilot_turn(message: "cl.Message") -> None:
    """Entry point for every chat message while Autopilot is active."""
    _ensure_project_root_on_path()
    from agents.autopilot_agent import is_exit_command

    content = (message.content or "").strip()
    low = content.lower()

    if is_exit_command(content):
        _autopilot_clear()
        await _emit_pipeline_event("dismiss")
        await cl.Message(content=(
            "🛑 **Autopilot disabled.** Back to normal chat. "
            "Click **Autopilot** on the dashboard to start again."
        )).send()
        return

    # Start / (re)enable
    if low.startswith("/autopilot"):
        rest = content[len("/autopilot"):].strip()
        cl.user_session.set("autopilot_active", True)
        _autopilot_save({
            "phase": "await_request", "baseline": "", "program": "",
            "hint": "", "iteration": 0, "applied": [], "snapshots": {},
            "last_sig": "", "repeat": 0, "started_compare": False,
            "current_failed": [],
        })
        if rest:
            await _autopilot_handle_request(rest, message.elements)
        else:
            await cl.Message(content=(
                "🛫 **Autopilot enabled.**\n\n"
                "Tell me what to fix in your own words — you can be specific "
                "or vague. For example:\n"
                "• *\"P50070-HPPL486P files aren't matching, check it\"*\n"
                "• *\"the timestamp column is wrong, should be dd-MM-yyyy\"*\n"
                "• paste differing data, or attach a spec/field-mapping file.\n\n"
                "I'll read the files, understand the differences, apply fixes, "
                "run → unload → compare, and repeat until they match — asking "
                "you only when I'm unsure. Send `/autopilot stop` anytime."
            )).send()
        return

    # Active session — route by phase
    state = _autopilot_state()
    phase = state.get("phase")
    if phase == "await_request" or not state.get("baseline"):
        await _autopilot_handle_request(content, message.elements)
    elif phase == "await_user":
        await _autopilot_resume_with_hint(content, message.elements)
    else:
        # Mid-run message = an extra hint; fold it and keep going.
        await _autopilot_resume_with_hint(content, message.elements)

async def _autopilot_write_ims_queries(program: str) -> None:
    """Author ExperLib SELECT queries for a program's IMS reads.
 
    Read-only Autopilot intent: no baseline, no compare/fix loop. Reads the
    program's COBOL + IMS DBD metadata and asks the IMS-skilled
    AnalysisAgent to write the queries. Autopilot stays active afterwards.
    """
    from agents.autopilot_agent import gather_ims_artifacts
 
    await cl.Message(content=(
        f"🧩 **Writing IMS queries for program `{program}`** — reading the "
        f"COBOL and IMS DBD metadata…"
    )).send()
 
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, gather_ims_artifacts, program)
 
    if not data.get("ok"):
        await cl.Message(content=(
            f"⚠️ {data.get('error', 'Could not gather the required files.')}"
        )).send()
        return
 
    segs = data.get("segments") or []
    if segs:
        await cl.Message(content=(
            f"🔎 Found {len(data['artifacts'])} metadata file(s); segments: "
            f"`{', '.join(segs)}`. Asking the IMS skill to author queries…"
        )).send()
 
    try:
        agent = get_analysis_agent()
        answer = await agent.write_ims_queries(program, data["artifacts"])
    except Exception as exc:  # noqa: BLE001 — surface, never crash autopilot
        await cl.Message(content=(
            f"❌ Failed to generate IMS queries: `{exc}`"
        )).send()
        return
 
    await cl.Message(content=answer).send()
    await cl.Message(content=(
        "_Autopilot is still active. Send another program, give a baseline "
        "to validate, or `/autopilot stop` to exit._"
    )).send()

async def _autopilot_handle_request(text: str, elements) -> None:
    """Interpret the user's initial request, then start the drive loop."""
    from agents.autopilot_agent import (
        detect_ims_query_request, gather_ims_artifacts, interpret_request,
        load_hint, save_hint,
    )
 
    # Program-scoped "write IMS queries for program XYZ" — read-only, no
    # baseline and no compare/fix loop. Handle before baseline parsing.
    ims_program = detect_ims_query_request(text)
    if ims_program:
        await _autopilot_write_ims_queries(ims_program)
        return

    files_txt = _autopilot_extract_file_guidance(elements)
    active_baseline = cl.user_session.get("chat_active_baseline") or None

    plan = await interpret_request(text, active_baseline)
    baseline = plan["baseline"]
    state = _autopilot_state()

    if plan["need_baseline"] or not baseline:
        # Stash any guidance given so far, then ask for the baseline.
        pending = "\n".join(
            h for h in [state.get("hint", ""), plan.get("hint", ""), files_txt] if h
        ).strip()
        state["hint"] = pending
        await _autopilot_ask(state, plan["question"])
        return

    program = _extract_program_name_safe(baseline)
    remembered = load_hint(baseline)
    combined = "\n".join(
        h for h in [remembered, state.get("hint", ""), plan.get("hint", ""), files_txt]
        if h
    ).strip()

    state.update({
        "phase": "running", "baseline": baseline, "program": program,
        "hint": combined, "iteration": 0, "applied": [], "snapshots": {},
        "last_sig": "", "repeat": 0, "started_compare": False,
        "current_failed": [], "explain_only": bool(plan.get("explain_only")),
    })
    _autopilot_save(state)
    save_hint(baseline, combined)

    mode = (
        "explain-only — I will diagnose the root cause and NOT change any code"
        if plan.get("explain_only") else None
    )
    intro = f"🛫 **Autopilot engaged** for `{baseline}` (program `{program}`)."
    if mode:
        intro += f"\n\n**Mode:** {mode}."
    if combined:
        intro += "\n\n**Using your guidance:**\n> " + combined.replace("\n", "\n> ")
    await cl.Message(content=intro).send()

    await _autopilot_drive()


async def _autopilot_resume_with_hint(text: str, elements) -> None:
    """Fold a mid-loop answer/hint into the guidance and resume driving."""
    from agents.autopilot_agent import save_hint

    state = _autopilot_state()
    if not state.get("baseline"):
        # We were waiting for a baseline — re-interpret this message.
        await _autopilot_handle_request(text, elements)
        return

    files_txt = _autopilot_extract_file_guidance(elements)
    new_hint = "\n".join(
        h for h in [state.get("hint", ""), text, files_txt] if h
    ).strip()
    state["hint"] = new_hint
    state["repeat"] = 0  # new guidance → give it a fresh chance
    _autopilot_save(state)
    save_hint(state["baseline"], new_hint)

    await cl.Message(content="✅ Got it — folding that into my approach and continuing…").send()
    await _autopilot_drive()


async def _autopilot_drive() -> None:
    """Core loop: ensure compare → analyse (guided) → apply → run →
    unload → compare, repeating until match, or pausing to ask."""
    from pathlib import Path as _P
    _ensure_project_root_on_path()
    from agents.autopilot_agent import (
        MAX_ITERATIONS, REPEAT_LIMIT, compose_guided_diff_details,
        save_hint, should_pause_for_help,
    )
    from prompts.skills.compare import (
        build_diff_details, pick_top_failed_file, read_failed_file,
    )

    state = _autopilot_state()
    baseline = state["baseline"]
    program = state["program"]
    session = session_manager.get_session(baseline)
    orchestrator = get_orchestrator()
    agent = get_analysis_agent()
    loop = asyncio.get_event_loop()

    state["phase"] = "running"
    _autopilot_save(state)

    # Explain-only: diagnose + propose, never touch code.
    if state.get("explain_only"):
        await _autopilot_explain(state)
        return

    # ── Step 0: compare-first autonomy ───────────────────────────────
    if not state.get("started_compare"):
        await cl.Message(content=(
            "🧭 Running compare to see the current differences…"
        )).send()
        cmp_ok, cmp_res = await _ap_run(loop, orchestrator, "compare", session)
        state["started_compare"] = True
        if not cmp_ok:
            await cl.Message(content=(
                f"❌ Compare failed: `{cmp_res.get('message', '?')}`. Stopping."
            )).send()
            _autopilot_clear()
            return
        if cmp_res.get("all_pass"):
            await cl.Message(content=(
                "🎉 Files already match — nothing to fix."
            )).send()
            _autopilot_clear()
            return
        state["current_failed"] = cmp_res.get("failed_files", []) or []
        _autopilot_save(state)

    current_failed = state.get("current_failed") or []

    while state["iteration"] < MAX_ITERATIONS:
        state["iteration"] += 1
        i = state["iteration"]

        top = pick_top_failed_file(current_failed)
        if not top:
            await cl.Message(content="ℹ️ No failing files remain to analyse.").send()
            break

        record = read_failed_file(baseline, top)
        if not record["has_delta"]:
            _autopilot_save(state)
            await _autopilot_ask(state, (
                f"I can't read a usable diff for `{top}`. Paste the differing "
                f"values (expected vs generated) or describe what's wrong."
            ))
            return

        diff_details = compose_guided_diff_details(
            build_diff_details(record), state.get("hint", "")
        )
        await cl.Message(content=(
            f"🔎 **Iter {i}/{MAX_ITERATIONS}** — analysing `{top}`"
            + (" with your guidance…" if state.get("hint") else "…")
        )).send()

        try:
            suggestion = await agent.analyze_output_diff(
                baseline_name=baseline, program_name=program,
                diff_details=diff_details, failed_files=current_failed,
            )
        except Exception as exc:  # noqa: BLE001
            _autopilot_save(state)
            await _autopilot_ask(state, (
                f"My analysis call failed: `{exc}`. Add a hint and I'll retry."
            ))
            return

        conf = suggestion.confidence or 0
        actionable = bool(
            suggestion.file_to_fix
            and suggestion.original_code
            and suggestion.suggested_code
        )
        reason = should_pause_for_help(conf, state.get("repeat", 0))
        if reason or not actionable:
            rc = suggestion.root_cause or "unclear"
            detail = (
                f"**What I found:** {rc}\n\n"
                f"**Why I'm pausing:** "
                + (reason or "I couldn't form a concrete code change")
                + ".\n\nTell me which field/format is right, paste the correct "
                "value, or confirm the approach — and I'll continue."
            )
            _autopilot_save(state)
            await _autopilot_ask(state, detail)
            return

        # Snapshot → apply
        try:
            before = _P(suggestion.file_to_fix).read_bytes()
        except OSError as exc:
            _autopilot_save(state)
            await _autopilot_ask(state, (
                f"I can't read `{suggestion.file_to_fix}` to edit it safely "
                f"({exc}). Check the path, then reply to continue."
            ))
            return

        apply_result = apply_fixes_to_file(
            file_path=suggestion.file_to_fix,
            edits=[(e.original_code, e.suggested_code) for e in suggestion.get_edits()],
        )
        if not apply_result.ok:
            state["repeat"] = state.get("repeat", 0) + 1
            await cl.Message(content=(
                f"⚠️ Iter {i}: couldn't apply automatically "
                f"({apply_result.message}). Re-analysing…"
            )).send()
            if state["repeat"] >= REPEAT_LIMIT:
                _autopilot_save(state)
                await _autopilot_ask(state, (
                    "I keep failing to land this edit cleanly. Paste the exact "
                    "current code for the area, or the fix you want."
                ))
                return
            _autopilot_save(state)
            continue

        state["snapshots"].setdefault(suggestion.file_to_fix, before)
        try:
            xrf = await asyncio.to_thread(
                update_xrf_from_cs_edit, suggestion.file_to_fix
            )
            if not (xrf.ok or xrf.skipped):
                logger.warning(f"autopilot iter {i} XRF: {xrf.message}")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"autopilot iter {i} XRF skipped: {exc}")

        await cl.Message(content=(
            f"✨ Iter {i}: wrote fix to `{suggestion.file_to_fix}` "
            f"(line {apply_result.line_start}, confidence {conf}%). Running…"
        )).send()

        iter_rec = {
            "iter": i, "file": suggestion.file_to_fix,
            "line": apply_result.line_start, "confidence": conf,
            "root_cause": suggestion.root_cause,
            "original": suggestion.original_code,
            "suggested": suggestion.suggested_code, "status": "applied",
        }

        # Run
        run_ok, run_res = await _ap_run(loop, orchestrator, "run", session)
        if not run_ok:
            try:
                _P(suggestion.file_to_fix).write_bytes(before)
                if not any(
                    r.get("file") == suggestion.file_to_fix
                    for r in state["applied"]
                ):
                    state["snapshots"].pop(suggestion.file_to_fix, None)
            except OSError as exc:
                logger.error(f"autopilot iter {i} rollback FAILED: {exc}")
            err = ((run_res or {}).get("error_info") or {}).get(
                "error_message", "?"
            )
            # Self-correct: remember to avoid this, then retry.
            state["hint"] = (state.get("hint", "") + (
                f"\nAVOID: a previous change caused this build/run error: {err}"
            )).strip()
            state["repeat"] = state.get("repeat", 0) + 1
            await cl.Message(content=(
                f"⚠️ Iter {i}: program failed after fix — reverted. "
                f"I'll avoid that approach. Error: `{err}`"
            )).send()
            if state["repeat"] >= REPEAT_LIMIT:
                _autopilot_save(state)
                await _autopilot_ask(state, (
                    "My edits keep breaking the build. Can you point me at the "
                    "right method/field or paste the correct snippet?"
                ))
                return
            _autopilot_save(state)
            continue

        await cl.Message(content=f"✅ Iter {i}: ran OK. Unloading…").send()
        unload_ok, unload_res = await _ap_run(loop, orchestrator, "unload", session)
        if not unload_ok:
            _autopilot_save(state)
            await _autopilot_ask(state, (
                f"Unload failed (`{unload_res}`) so compare would be stale. "
                f"Resolve it, then reply to continue."
            ))
            return

        await cl.Message(content=f"✅ Iter {i}: unload done. Comparing…").send()
        cmp_ok, cmp_res = await _ap_run(loop, orchestrator, "compare", session)
        if not cmp_ok:
            _autopilot_save(state)
            await _autopilot_ask(state, (
                f"Compare crashed (`{cmp_res.get('message', '?')}`). "
                f"Reply to retry or send `/autopilot stop`."
            ))
            return

        if cmp_res.get("all_pass"):
            iter_rec["status"] = "verified"
            state["applied"].append(iter_rec)
            try:
                agent.mark_fix_success(baseline)
            except Exception:  # noqa: BLE001
                pass
            save_hint(baseline, state.get("hint", ""))
            await _autopilot_success(state, cmp_res)
            return

        iter_rec["status"] = "kept_progress"
        state["applied"].append(iter_rec)
        remaining = cmp_res.get("failed_files", []) or []
        fail_n = (
            cmp_res.get("after_csv_fail", 0) + cmp_res.get("root_mixed_fail", 0)
        )
        sig = "|".join(sorted(remaining)) + f":{fail_n}"
        if sig == state.get("last_sig"):
            state["repeat"] = state.get("repeat", 0) + 1
        else:
            state["repeat"] = 0
            state["last_sig"] = sig
        current_failed = remaining
        state["current_failed"] = remaining
        _autopilot_save(state)
        await cl.Message(content=(
            f"➡️ Iter {i}: {fail_n} difference(s) across {len(remaining)} "
            f"file(s) remain. Continuing…"
        )).send()

        if state["repeat"] >= REPEAT_LIMIT:
            await _autopilot_ask(state, (
                "The same differences persist after my changes. Give me a "
                "hint about the right field/format and I'll adjust."
            ))
            return

    await _autopilot_exhausted(state)


async def _autopilot_success(state: dict, cmp_res: dict) -> None:
    """Render the verified-success summary and offer finalize / revert."""
    state["phase"] = "done"
    _autopilot_save(state)
    cl.user_session.set("autopilot_active", False)

    baseline = state["baseline"]
    rows = []
    for r in state.get("applied", []):
        rows.append(
            f"**Iter {r['iter']} — `{r['file']}` @ line {r.get('line', '?')}** "
            f"({r.get('confidence', 0)}%)\n{r.get('root_cause', '')}"
        )
    body = "\n\n".join(rows) if rows else "(no fixes retained)"
    actions = [
        cl.Action(
            name="autopilot_finalize", payload={"baseline": baseline},
            label="🚀 Finalize Now",
        ),
        cl.Action(
            name="autopilot_revert_all", payload={"baseline": baseline},
            label="🗑️ Revert All",
        ),
    ]
    await cl.Message(content=(
        f"🎉 **Autopilot Verified — All Files Match**\n\n"
        f"**Baseline:** `{baseline}`\n"
        f"**Iterations:** {state.get('iteration', 0)} / "
        f"{_AUTOPILOT_MAX_ITERATIONS_LABEL()}\n"
        f"**AFTER CSV:** {cmp_res.get('after_csv_pass', 0)} pass, 0 fail ✅\n"
        f"**Root Mixed:** {cmp_res.get('root_mixed_pass', 0)} pass, 0 fail ✅\n\n"
        f"---\n\n### Fixes applied\n\n{body}\n\n---\n\n"
        f"Autopilot is now off. Finalize to deliver, or revert everything."
    ), actions=actions).send()


def _AUTOPILOT_MAX_ITERATIONS_LABEL() -> int:
    from agents.autopilot_agent import MAX_ITERATIONS
    return MAX_ITERATIONS


async def _autopilot_exhausted(state: dict) -> None:
    """Render the stop-without-convergence summary."""
    state["phase"] = "done"
    _autopilot_save(state)
    cl.user_session.set("autopilot_active", False)

    baseline = state["baseline"]
    retained = len(state.get("applied", []))
    actions = [
        cl.Action(
            name="autopilot_revert_all", payload={"baseline": baseline},
            label="🗑️ Revert All",
        ),
    ]
    await cl.Message(content=(
        f"⚠️ **Autopilot stopped without full match**\n\n"
        f"**Baseline:** `{baseline}`\n"
        f"**Iterations:** {state.get('iteration', 0)}\n"
        f"**Fixes retained on disk:** {retained}\n\n"
        f"Re-enable Autopilot with a more specific hint, review manually, or "
        f"revert everything the agent wrote."
    ), actions=actions).send()


async def _autopilot_explain(state: dict) -> None:
    """Explain-only mode: run compare (read-only on source), analyse the
    top failing files, and present the root cause + a PROPOSED fix WITHOUT
    applying anything. Offers a button to proceed to fixing."""
    _ensure_project_root_on_path()
    from agents.autopilot_agent import compose_guided_diff_details
    from prompts.skills.compare import (
        build_diff_details, read_failed_file,
    )

    baseline = state["baseline"]
    program = state["program"]
    session = session_manager.get_session(baseline)
    orchestrator = get_orchestrator()
    agent = get_analysis_agent()
    loop = asyncio.get_event_loop()

    await cl.Message(content=(
        "🔎 **Explain-only mode** — I'll diagnose the root cause and will "
        "**NOT** change any code.\n\nRunning compare to see the differences…"
    )).send()
    cmp_ok, cmp_res = await _ap_run(loop, orchestrator, "compare", session)
    if not cmp_ok:
        await cl.Message(content=(
            f"❌ Compare failed: `{cmp_res.get('message', '?')}`."
        )).send()
        _autopilot_clear()
        return
    if cmp_res.get("all_pass"):
        await cl.Message(content=(
            "✅ Files already match — there are no differences to explain."
        )).send()
        _autopilot_clear()
        return

    failed = cmp_res.get("failed_files", []) or []
    shown = 0
    for top in failed[:3]:
        record = read_failed_file(baseline, top)
        if not record["has_delta"]:
            continue
        diff_details = compose_guided_diff_details(
            build_diff_details(record), state.get("hint", "")
        )
        try:
            s = await agent.analyze_output_diff(
                baseline_name=baseline, program_name=program,
                diff_details=diff_details, failed_files=failed,
            )
        except Exception as exc:  # noqa: BLE001
            await cl.Message(content=(
                f"⚠️ Could not analyse `{top}`: `{exc}`."
            )).send()
            continue
        code = ""
        if s.original_code and s.suggested_code:
            code = (
                "\n**Current code:**\n```csharp\n" + s.original_code +
                "\n```\n**Would need to become:**\n```csharp\n" +
                s.suggested_code + "\n```\n"
                + s.extra_edits_markdown()
            )
        await cl.Message(content=(
            f"### `{top}` — root cause\n\n"
            f"**{s.root_cause}**\n\n{s.explanation}\n\n"
            f"**File:** `{s.file_to_fix}`"
            + (f" (line {s.line_number})" if s.line_number else "")
            + f"\n{code}"
        )).send()
        shown += 1

    if shown == 0:
        await cl.Message(content=(
            "I couldn't produce a confident root cause from the current "
            "diff. Add a hint (which field/format) and ask again."
        )).send()

    state["phase"] = "done"
    _autopilot_save(state)
    cl.user_session.set("autopilot_active", False)

    actions = [
        cl.Action(
            name="autopilot_proceed_fix",
            payload={"baseline": baseline, "hint": state.get("hint", "")},
            label="🚀 Proceed to Fix",
        ),
    ]
    await cl.Message(content=(
        "📋 **Explanation complete — no code was changed.**\n\n"
        "Click **Proceed to Fix** to let Autopilot apply and verify the "
        "fix, or send `/autopilot stop`."
    ), actions=actions).send()


@cl.action_callback("autopilot_proceed_fix")
async def on_autopilot_proceed_fix(action: cl.Action):
    """Switch from explain-only to the real fix loop on user request."""
    baseline = action.payload.get("baseline")
    hint = action.payload.get("hint", "")
    program = _extract_program_name_safe(baseline)
    cl.user_session.set("autopilot_active", True)
    _autopilot_save({
        "phase": "running", "baseline": baseline, "program": program,
        "hint": hint, "iteration": 0, "applied": [], "snapshots": {},
        "last_sig": "", "repeat": 0, "started_compare": False,
        "current_failed": [], "explain_only": False,
    })
    await cl.Message(content=(
        f"🛫 Proceeding to fix `{baseline}` — I'll apply, run, unload and "
        f"re-compare until it matches."
    )).send()
    await _autopilot_drive()


@cl.action_callback("autopilot_revert_all")
async def on_autopilot_revert_all(action: cl.Action):
    """Restore every file Autopilot touched to its pre-session bytes."""
    from pathlib import Path as _P
    state = _autopilot_state()
    snapshots = state.get("snapshots") or {}
    if not snapshots:
        await cl.Message(content=(
            "ℹ️ Nothing to revert — no Autopilot snapshots recorded."
        )).send()
        _autopilot_clear()
        return
    reverted, failed = [], []
    for path, before in snapshots.items():
        try:
            _P(path).write_bytes(before)
            reverted.append(path)
        except OSError as exc:
            failed.append(f"{path}: {exc}")
    _autopilot_clear()
    body = "\n".join(f"• `{p}`" for p in reverted) if reverted else "(none)"
    err = ("\n\n**Failed:**\n" + "\n".join(f"• {e}" for e in failed)) if failed else ""
    await cl.Message(content=(
        f"🗑️ **Reverted {len(reverted)} file(s)**\n\n{body}{err}"
    )).send()


@cl.action_callback("autopilot_finalize")
async def on_autopilot_finalize(action: cl.Action):
    """Autopilot verified — jump straight to finalize."""
    baseline = action.payload.get("baseline")
    _autopilot_clear()
    await handle_finalize(baseline)


# ─── Validate action callbacks (Compare accept/reject + push/PR gates) ──

async def _render_compare_analysis_card(
    baseline_name: str,
    program_name: str,
    failed_files: list,
    validate_mode: bool,
) -> None:
    """Analyse the top failing compare file and render a suggestion card.

    Reuses the Run-failure suggestion-card buttons but stamps each
    payload with ``compare_mode=True`` so the Fix-Applied / Auto-Apply
    callbacks resume via rerun→compare instead of just rerun.
    """
    _ensure_project_root_on_path()
    from prompts.skills.compare import (
        build_diff_details,
        pick_top_failed_file,
        read_failed_file,
    )

    top = pick_top_failed_file(failed_files or [])
    if not top:
        await cl.Message(content=(
            "ℹ️ **Nothing to analyse** — compare reported differences but "
            "no specific failing file name was recorded. Review the HTML "
            "report manually."
        )).send()
        return

    record = read_failed_file(baseline_name, top)
    if not record["has_delta"]:
        html = record.get("html_path") or ""
        await cl.Message(content=(
            f"ℹ️ **Delta snippet missing** for `{top}`.\n\n"
            f"The CompareX `-delta.txt` artefact was not found under the "
            f"baseline REPORT folder. Open the Beyond Compare HTML report "
            f"to review the differences manually:\n\n"
            f"{html or '(HTML report not found either)'}"
        )).send()
        return

    diff_details = build_diff_details(record)
    left_n = len(record["left_only"])
    right_n = len(record["right_only"])
    html_hint = (
        f"\n\n📄 **Full HTML diff:** `{record['html_path']}`"
        if record.get("html_path") else ""
    )

    await cl.Message(content=(
        f"🤖 **AI Analysis Started — Compare Differences**\n\n"
        f"**Baseline:** `{baseline_name}`\n"
        f"**Analysing file:** `{top}`\n"
        f"**Missing records (in baseline, not generated):** {left_n}\n"
        f"**Extra records (generated, not in baseline):** {right_n}"
        f"{html_hint}\n\n"
        f"⏳ Reasoning over COBOL spec + C# source…"
    )).send()

    try:
        analysis_agent = get_analysis_agent()
        suggestion = await analysis_agent.analyze_output_diff(
            baseline_name=baseline_name,
            program_name=program_name,
            diff_details=diff_details,
            failed_files=failed_files or [top],
        )
    except Exception as exc:  # noqa: BLE001 — surface failure to UI
        logger.error(f"analyze_output_diff failed: {exc}")
        await cl.Message(content=(
            f"❌ **AI Analysis Failed**\n\n**Error:** `{exc}`\n\n"
            f"Fix manually and re-run `compare {baseline_name}`."
        )).send()
        return

    confidence_emoji = (
        "🟢" if suggestion.confidence >= 70
        else "🟡" if suggestion.confidence >= 50 else "🔴"
    )
    confidence_text = (
        "High" if suggestion.confidence >= 70
        else "Medium" if suggestion.confidence >= 50 else "Low"
    )

    code_section = ""
    if suggestion.original_code and suggestion.suggested_code:
        code_section = (
            "\n---\n\n**❌ Current Code:**\n```csharp\n"
            + suggestion.original_code
            + "\n```\n\n**✅ Suggested Fix:**\n```csharp\n"
            + suggestion.suggested_code
            + "\n```\n"
            + suggestion.extra_edits_markdown()
        )

    similar_fixes_text = ""
    if suggestion.similar_fixes_used:
        similar_fixes_text = "\n**📚 Similar Past Fixes:**\n"
        for fix in suggestion.similar_fixes_used[:3]:
            similar_fixes_text += f"• PR #{fix.pr_id}: `{fix.baseline}`\n"

    line_info = (
        f"\n**Line Number:** {suggestion.line_number}"
        if suggestion.line_number else ""
    )
    review_warning = (
        "\n⚠️ **Manual review recommended** (confidence below 70%)\n"
        if suggestion.requires_manual_review else ""
    )

    sess = analysis_agent.get_session(baseline_name)
    attempt_info = (
        f" (Attempt {sess.current_attempt}/{sess.max_attempts})" if sess else ""
    )

    result_message = (
        f"🤖 **AI Analysis Complete — Compare Differences**{attempt_info}\n\n"
        f"**Program:** `{program_name}`\n"
        f"**Baseline:** `{baseline_name}`\n"
        f"**File:** `{top}`\n"
        f"**Confidence:** {confidence_emoji} {suggestion.confidence}% "
        f"({confidence_text})\n{review_warning}\n---\n\n"
        f"**🔍 Root Cause:**\n{suggestion.root_cause}\n\n---\n\n"
        f"**📋 Analysis:**\n{suggestion.explanation}\n\n---\n\n"
        f"**📁 File to Fix:** `{suggestion.file_to_fix}`{line_info}\n"
        f"{code_section}\n---\n\n"
        f"**🏷️ Fix Type:** `{suggestion.fix_type}`\n{similar_fixes_text}\n---\n\n"
        f"**📝 Notes:**\n{suggestion.additional_notes or 'None'}\n\n---\n\n"
        f"**Loop:** Click **✨ Auto Apply** (writes the change) or edit "
        f"manually, then **✅ Fix Applied — Rerun & Compare** to re-run "
        f"and re-compare. The loop repeats until all files match or you "
        f"accept the remaining differences."
    )

    actions = [
        cl.Action(
            name="retry_analysis_compare",
            payload={
                "baseline": baseline_name,
                "program_name": program_name,
                "failed_files": failed_files or [top],
                "validate_mode": validate_mode,
            },
            label=f"🔄 Re-analyse{attempt_info}",
        ),
        cl.Action(
            name="auto_apply_fix",
            payload={
                "baseline": baseline_name,
                "program_name": program_name,
                "file_to_fix": suggestion.file_to_fix,
                "original_code": suggestion.original_code or "",
                "suggested_code": suggestion.suggested_code or "",
                "edits": _edits_payload(suggestion),
                "validate_mode": validate_mode,
                "compare_mode": True,
            },
            label="✨ Auto Apply",
        ),
        cl.Action(
            name="mark_fix_applied",
            payload={
                "baseline": baseline_name,
                "program_name": program_name,
                "file_to_fix": suggestion.file_to_fix,
                "validate_mode": validate_mode,
                "compare_mode": True,
            },
            label="✅ Fix Applied — Rerun & Compare",
        ),
    ]
    await cl.Message(content=result_message, actions=actions).send()


async def _run_then_compare_standalone(
    baseline_name: str, program_name: str,
) -> None:
    """Standalone fix-loop iteration: run → compare → decide."""
    session = session_manager.get_session(baseline_name)
    orchestrator = get_orchestrator()
    loop = asyncio.get_event_loop()

    run_future = loop.run_in_executor(
        None,
        lambda: asyncio.run(orchestrator.run_skill("run", session)),
    )
    async with ProgressCard(
        f"Re-run {program_name}", baseline_name,
        interval=15, eta_seconds=180,
    ) as card:
        await card.wait_for(run_future, status="Re-executing after fix…")
        run_ok, run_result = await run_future
        if run_ok:
            await card.complete("Run complete")
        else:
            err = ((run_result or {}).get("error_info") or {}).get(
                "error_message", "Failed"
            )
            await card.fail(err)

    if not run_ok:
        run_result = run_result or {}
        err_info = run_result.get("error_info") or {}
        actions = [cl.Action(
            name="analyse_with_ai",
            payload={
                "baseline": baseline_name,
                "program_name": program_name,
                "error_message": err_info.get("error_message", "Unknown error"),
                "error_type": err_info.get("error_type", "unknown"),
                "stack_trace": err_info.get("stack_trace", ""),
                "exit_code": run_result.get("exit_code", 1),
                "validate_mode": False,
            },
            label="🤖 Analyse with AI",
        )]
        await cl.Message(content=(
            f"❌ **Run Failed After Fix**\n\n"
            f"The code change caused the program to error — fix the new "
            f"issue first:\n\n"
            f"```\n{err_info.get('error_message', 'Unknown error')}\n```"
        ), actions=actions).send()
        return

    # Unload BEFORE compare so compare-x diffs the freshly produced output,
    # not stale files. Mirrors the validate/reporting run→unload→compare order.
    unload_future = loop.run_in_executor(
        None,
        lambda: asyncio.run(orchestrator.run_skill("unload", session)),
    )
    async with ProgressCard(
        f"Unload {baseline_name}", baseline_name,
        interval=10, eta_seconds=60,
    ) as card:
        await card.wait_for(unload_future, status="Unloading fresh data from DB…")
        unload_ok, unload_result = await unload_future
        if unload_ok:
            await card.complete("Unload complete")
        else:
            await card.fail(str(unload_result) or "Unload failed")

    if not unload_ok:
        await cl.Message(content=(
            f"❌ **Unload Failed**\n\n"
            f"Compare would read stale data — stopping.\n\n"
            f"```\n{unload_result}\n```"
        )).send()
        return

    cmp_future = loop.run_in_executor(
        None,
        lambda: asyncio.run(orchestrator.run_skill("compare", session)),
    )
    async with ProgressCard(
        f"Re-compare {baseline_name}", baseline_name,
        interval=10, eta_seconds=60,
    ) as card:
        await card.wait_for(cmp_future, status="Running compare-x again…")
        cmp_ok, cmp_result = await cmp_future
        if cmp_ok:
            await card.complete("Compare complete")
        else:
            await card.fail(cmp_result.get("message") or "Compare failed")

    if not cmp_ok:
        await cl.Message(content=(
            f"❌ **Compare Failed**\n\n"
            f"```\n{cmp_result.get('message','?')}\n```"
        )).send()
        return

    if cmp_result.get("all_pass"):
        analysis_agent = get_analysis_agent()
        try:
            analysis_agent.mark_fix_success(baseline_name)
        except Exception:  # noqa: BLE001
            pass
        await cl.Message(content=(
            f"🎉 **All Files Match!**\n\n"
            f"**Baseline:** `{baseline_name}`\n"
            f"**AFTER CSV:** {cmp_result.get('after_csv_pass',0)} pass, "
            f"0 fail ✅\n"
            f"**Root Mixed:** {cmp_result.get('root_mixed_pass',0)} pass, "
            f"0 fail ✅\n\n"
            f"The AI fix brought generated output in line with the "
            f"expected baseline.\n\n"
            f"**Next:** Run `finalize {baseline_name}` (Step 8)."
        )).send()
        return

    remaining = cmp_result.get("failed_files", []) or []
    failed_list = (
        "\n".join([f"• {f}" for f in remaining]) if remaining else "• (unknown)"
    )
    report_path = cmp_result.get("report_path", "")
    actions = [
        cl.Action(
            name="compare_acceptable",
            payload={"baseline": baseline_name},
            label="✅ Acceptable (stop loop)",
        ),
        cl.Action(
            name="compare_not_acceptable",
            payload={"baseline": baseline_name},
            label="❌ Not Acceptable (stop loop)",
        ),
    ]
    await cl.Message(content=(
        f"⚠️ **Differences Remain After Fix**\n\n"
        f"**AFTER CSV:** {cmp_result.get('after_csv_pass',0)} pass, "
        f"{cmp_result.get('after_csv_fail',0)} fail\n"
        f"**Root Mixed:** {cmp_result.get('root_mixed_pass',0)} pass, "
        f"{cmp_result.get('root_mixed_fail',0)} fail\n\n"
        f"**Still differing:**\n{failed_list}\n\n"
        f"**Report:** `{report_path}`\n\n"
        f"Analysing the next file… (or stop the loop below)"
    ), actions=actions).send()

    await _render_compare_analysis_card(
        baseline_name=baseline_name,
        program_name=program_name,
        failed_files=remaining,
        validate_mode=False,
    )


@cl.action_callback("analyse_compare_diff")
async def on_analyse_compare_diff(action: cl.Action):
    """Entry point into the compare auto-fix loop (standalone + validate)."""
    baseline_name = action.payload.get("baseline")
    failed_files = action.payload.get("failed_files") or []
    validate_mode = bool(action.payload.get("validate_mode", False))
    program_name = (
        action.payload.get("program_name")
        or _extract_program_name_safe(baseline_name)
    )
    await _render_compare_analysis_card(
        baseline_name=baseline_name,
        program_name=program_name,
        failed_files=failed_files,
        validate_mode=validate_mode,
    )


# ═════════════════════════════════════════════════════════════════════
# Autonomous Agent Auto-Fix — Copilot-style verify-before-suggest loop
# ═════════════════════════════════════════════════════════════════════
# Picks top failing file → analyses → snapshots the target file → auto-
# applies → runs the program → re-compares. Repeats up to
# ``_AUTOFIX_MAX_ITERATIONS``. Only surfaces the final verdict (verified
# fix, or exhausted attempts summary) to the user. Latest write is
# rolled back on run-failure; successful progress is retained between
# iterations.

def _load_autofix_max_iterations(default: int = 5) -> int:
    """Read ``agentic.autofix_max_iterations`` from the orchestration config."""
    try:
        import yaml
        cfg_path = (
            config.BASE_DIR / "prompts" / "skills"
            / "orchestration" / "_config.yaml"
        )
        doc = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        value = int((doc.get("agentic") or {}).get("autofix_max_iterations", default))
        return value if value > 0 else default
    except Exception as exc:  # noqa: BLE001 — config read is best-effort
        logger.warning(f"autofix_max_iterations read failed ({exc}); using {default}")
        return default


_AUTOFIX_MAX_ITERATIONS = _load_autofix_max_iterations()

# Per-baseline bookkeeping for the Revert All / Finalize buttons shown
# at the end of an autonomous run. Keyed by baseline name, cleared when
# the user picks a terminal action.
_AUTOFIX_STATE: Dict[str, Dict[str, Any]] = {}


async def _autofix_render_success(
    baseline_name: str,
    program_name: str,
    cmp_result: Dict[str, Any],
    state: Dict[str, Any],
    validate_mode: bool,
) -> None:
    """Render the final success message after an autonomous loop converges."""
    applied = state.get("applied") or []
    summary_lines = []
    for rec in applied:
        orig = rec.get("original") or ""
        sug = rec.get("suggested") or ""
        summary_lines.append(
            f"**Iter {rec['iter']} — `{rec['file']}` @ line "
            f"{rec.get('line','?')}** (confidence {rec.get('confidence',0)}%)\n\n"
            f"Root cause: {rec.get('root_cause','(n/a)')}\n\n"
            f"```csharp\n// before\n{orig}\n```\n"
            f"```csharp\n// after\n{sug}\n```"
        )
    body = "\n\n---\n\n".join(summary_lines) if summary_lines else "(no fixes retained)"

    content = (
        f"🎉 **Agent Auto-Fix Verified — All Files Match**\n\n"
        f"**Baseline:** `{baseline_name}`\n"
        f"**Program:** `{program_name}`\n"
        f"**Iterations used:** {len(state.get('iterations') or [])} / "
        f"{_AUTOFIX_MAX_ITERATIONS}\n"
        f"**AFTER CSV:** {cmp_result.get('after_csv_pass',0)} pass, 0 fail ✅\n"
        f"**Root Mixed:** {cmp_result.get('root_mixed_pass',0)} pass, 0 fail ✅\n\n"
        f"---\n\n"
        f"### Verified fixes applied\n\n{body}\n\n---\n\n"
        f"Each change was tested internally (program ran successfully and "
        f"re-compare matched the expected baseline). You can finalize now, "
        f"or revert everything the agent did if you want to review manually."
    )

    if validate_mode:
        actions = [
            cl.Action(
                name="autofix_resume_validate",
                payload={"baseline": baseline_name},
                label="▶️ Resume Validate Workflow",
            ),
            cl.Action(
                name="autofix_revert_all",
                payload={"baseline": baseline_name, "validate_mode": True},
                label="🗑️ Revert All Auto-Fixes",
            ),
        ]
    else:
        actions = [
            cl.Action(
                name="autofix_finalize_now",
                payload={"baseline": baseline_name},
                label="🚀 Finalize Now",
            ),
            cl.Action(
                name="autofix_revert_all",
                payload={"baseline": baseline_name, "validate_mode": False},
                label="🗑️ Revert All Auto-Fixes",
            ),
        ]
    await cl.Message(content=content, actions=actions).send()


async def _autofix_render_exhausted(
    baseline_name: str,
    program_name: str,
    state: Dict[str, Any],
    validate_mode: bool,
) -> None:
    """Render the summary when the autonomous loop stops without convergence."""
    iters = state.get("iterations") or []
    rows = []
    for rec in iters:
        rows.append(
            f"- Iter {rec['iter']}: `{rec.get('file','?')}` → "
            f"**{rec.get('status','?')}** "
            f"(confidence {rec.get('confidence',0)}%)"
        )
    log = "\n".join(rows) if rows else "(no iterations recorded)"
    retained = len(state.get("applied") or [])

    content = (
        f"⚠️ **Agent Auto-Fix Exhausted**\n\n"
        f"**Baseline:** `{baseline_name}`\n"
        f"**Program:** `{program_name}`\n"
        f"**Iterations tried:** {len(iters)} / {_AUTOFIX_MAX_ITERATIONS}\n"
        f"**Fixes retained on disk:** {retained}\n\n"
        f"---\n\n### Iteration log\n\n{log}\n\n---\n\n"
        f"Pick one:\n"
        f"- **🤖 Switch to Manual Mode** — step through suggestions one at a time\n"
        f"- **✅ Accept Remaining Diffs** — mark compare as acceptable with current code\n"
        f"- **🗑️ Revert All** — undo every change the agent wrote"
    )

    actions = [
        cl.Action(
            name="autofix_manual_mode",
            payload={
                "baseline": baseline_name,
                "program_name": program_name,
                "validate_mode": validate_mode,
            },
            label="🤖 Switch to Manual Mode",
        ),
        cl.Action(
            name="autofix_accept_remaining",
            payload={
                "baseline": baseline_name,
                "validate_mode": validate_mode,
            },
            label="✅ Accept Remaining Diffs",
        ),
        cl.Action(
            name="autofix_revert_all",
            payload={
                "baseline": baseline_name,
                "validate_mode": validate_mode,
            },
            label="🗑️ Revert All",
        ),
    ]
    await cl.Message(content=content, actions=actions).send()


async def _autonomous_fix_loop(
    baseline_name: str,
    program_name: str,
    failed_files: list,
    validate_mode: bool,
) -> None:
    """Copilot-style verify-before-suggest fix loop (max N iterations)."""
    from pathlib import Path as _P
    _ensure_project_root_on_path()
    from prompts.skills.compare import (
        build_diff_details,
        pick_top_failed_file,
        read_failed_file,
    )

    session = session_manager.get_session(baseline_name)
    orchestrator = get_orchestrator()
    agent = get_analysis_agent()

    state: Dict[str, Any] = {
        "applied": [],      # fixes retained on disk
        "snapshots": {},    # file_path -> original_bytes (for Revert All)
        "iterations": [],   # full per-iter audit log
    }
    _AUTOFIX_STATE[baseline_name] = state

    await cl.Message(content=(
        f"🚀 **Agent Auto-Fix Started**\n\n"
        f"**Baseline:** `{baseline_name}`\n"
        f"**Program:** `{program_name}`\n"
        f"**Max iterations:** {_AUTOFIX_MAX_ITERATIONS}\n"
        f"**Failing files:** {len(failed_files or [])} "
        f"({', '.join((failed_files or [])[:5])}{'…' if len(failed_files or []) > 5 else ''})\n\n"
        f"I will analyse → apply → run → re-compare internally and only "
        f"surface the final verified result. The latest change is reverted "
        f"if the program fails to run; progress is kept otherwise.\n\n"
        f"⏳ Working…"
    )).send()

    current_failed = list(failed_files or [])
    for i in range(1, _AUTOFIX_MAX_ITERATIONS + 1):
        top = pick_top_failed_file(current_failed)
        if not top:
            await cl.Message(content=(
                f"ℹ️ Iter {i}: no failing files left to analyse — stopping."
            )).send()
            break

        record = read_failed_file(baseline_name, top)
        if not record["has_delta"]:
            await cl.Message(content=(
                f"⚠️ Iter {i}: no `-delta.txt` artefact for `{top}` — stopping."
            )).send()
            state["iterations"].append(
                {"iter": i, "file": top, "status": "no_delta", "confidence": 0}
            )
            break

        diff_details = build_diff_details(record)
        left_n = len(record["left_only"])
        right_n = len(record["right_only"])
        await cl.Message(content=(
            f"🔎 **Iter {i}/{_AUTOFIX_MAX_ITERATIONS}** — analysing `{top}` "
            f"({left_n} missing / {right_n} extra records)…"
        )).send()

        try:
            suggestion = await agent.analyze_output_diff(
                baseline_name=baseline_name,
                program_name=program_name,
                diff_details=diff_details,
                failed_files=current_failed,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"autofix iter {i} analyse failed: {exc}")
            state["iterations"].append(
                {"iter": i, "file": top, "status": f"analyse_error:{exc}", "confidence": 0}
            )
            await cl.Message(content=(
                f"❌ Iter {i}: analysis call failed: `{exc}`. Stopping."
            )).send()
            break

        if (
            not suggestion.file_to_fix
            or not suggestion.original_code
            or not suggestion.suggested_code
        ):
            state["iterations"].append({
                "iter": i, "file": top, "status": "no_actionable_fix",
                "confidence": suggestion.confidence,
                "root_cause": suggestion.root_cause,
            })
            await cl.Message(content=(
                f"⚠️ Iter {i}: AI returned no actionable code snippet "
                f"(confidence {suggestion.confidence}%). Root cause: "
                f"_{suggestion.root_cause or 'unknown'}_. Stopping."
            )).send()
            break

        try:
            before_bytes = _P(suggestion.file_to_fix).read_bytes()
        except OSError as exc:
            state["iterations"].append({
                "iter": i, "file": suggestion.file_to_fix,
                "status": f"snapshot_failed:{exc}",
                "confidence": suggestion.confidence,
            })
            await cl.Message(content=(
                f"❌ Iter {i}: cannot read target file `{suggestion.file_to_fix}` "
                f"for snapshot: {exc}. Stopping."
            )).send()
            break

        apply_result = apply_fixes_to_file(
            file_path=suggestion.file_to_fix,
            edits=[(e.original_code, e.suggested_code) for e in suggestion.get_edits()],
        )
        if not apply_result.ok:
            state["iterations"].append({
                "iter": i, "file": suggestion.file_to_fix,
                "status": f"apply_failed:{apply_result.message}",
                "confidence": suggestion.confidence,
                "root_cause": suggestion.root_cause,
            })
            await cl.Message(content=(
                f"⚠️ Iter {i}: auto-apply refused "
                f"({apply_result.message}). Re-analysing with a different "
                f"approach next iteration…"
            )).send()
            continue

        iter_record: Dict[str, Any] = {
            "iter": i,
            "file": suggestion.file_to_fix,
            "line": apply_result.line_start,
            "root_cause": suggestion.root_cause,
            "original": suggestion.original_code,
            "suggested": suggestion.suggested_code,
            "confidence": suggestion.confidence,
            "status": "applied",
        }
        # Earlier snapshot wins (preserves pristine original across many edits).
        state["snapshots"].setdefault(suggestion.file_to_fix, before_bytes)

        try:
            xrf = await asyncio.to_thread(
                update_xrf_from_cs_edit, suggestion.file_to_fix
            )
            if not (xrf.ok or xrf.skipped):
                logger.warning(f"autofix iter {i} XRF: {xrf.message}")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"autofix iter {i} XRF skipped: {exc}")

        await cl.Message(content=(
            f"✨ Iter {i}: wrote fix to `{suggestion.file_to_fix}` "
            f"(line {apply_result.line_start}, confidence "
            f"{suggestion.confidence}%). Re-running program…"
        )).send()

        loop = asyncio.get_event_loop()
        try:
            run_ok, run_res = await loop.run_in_executor(
                None,
                lambda: asyncio.run(orchestrator.run_skill("run", session)),
            )
        except Exception as exc:  # noqa: BLE001
            run_ok, run_res = False, {"error_info": {"error_message": str(exc)}}

        if not run_ok:
            try:
                _P(suggestion.file_to_fix).write_bytes(before_bytes)
                # Snapshot removed only if no earlier iteration also edited this file.
                if not any(
                    r.get("file") == suggestion.file_to_fix
                    for r in state["applied"]
                ):
                    state["snapshots"].pop(suggestion.file_to_fix, None)
            except OSError as exc:
                logger.error(f"autofix iter {i} rollback FAILED: {exc}")
            err_info = (run_res or {}).get("error_info") or {}
            iter_record["status"] = "run_failed_reverted"
            iter_record["error"] = err_info.get("error_message", "?")
            state["iterations"].append(iter_record)
            await cl.Message(content=(
                f"⚠️ Iter {i}: program failed after fix — reverted. "
                f"Error: `{err_info.get('error_message','?')}`. "
                f"Trying a different angle next iteration…"
            )).send()
            continue

        await cl.Message(content=(
            f"✅ Iter {i}: program ran OK. Unloading fresh data from DB…"
        )).send()

        # Unload BEFORE compare: the run writes to DB; unload pulls the
        # fresh output files the compare then diffs. Skipping it compares
        # stale files. Mirrors the validate/reporting step order.
        try:
            unload_ok, unload_res = await loop.run_in_executor(
                None,
                lambda: asyncio.run(orchestrator.run_skill("unload", session)),
            )
        except Exception as exc:  # noqa: BLE001
            unload_ok, unload_res = False, str(exc)

        if not unload_ok:
            iter_record["status"] = "unload_failed"
            iter_record["error"] = str(unload_res)
            state["iterations"].append(iter_record)
            state["applied"].append(iter_record)
            await cl.Message(content=(
                f"❌ Iter {i}: unload failed: `{unload_res}`. Stopping "
                f"(compare would read stale data)."
            )).send()
            break

        await cl.Message(content=(
            f"✅ Iter {i}: unload complete. Running compare-x…"
        )).send()

        try:
            cmp_ok, cmp_res = await loop.run_in_executor(
                None,
                lambda: asyncio.run(orchestrator.run_skill("compare", session)),
            )
        except Exception as exc:  # noqa: BLE001
            cmp_ok, cmp_res = False, {"message": str(exc)}

        if not cmp_ok:
            iter_record["status"] = "compare_crashed"
            iter_record["error"] = cmp_res.get("message", "?")
            state["iterations"].append(iter_record)
            state["applied"].append(iter_record)
            await cl.Message(content=(
                f"❌ Iter {i}: compare crashed: "
                f"`{cmp_res.get('message','?')}`. Stopping."
            )).send()
            break

        if cmp_res.get("all_pass"):
            iter_record["status"] = "verified"
            state["iterations"].append(iter_record)
            state["applied"].append(iter_record)
            try:
                agent.mark_fix_success(baseline_name)
            except Exception:  # noqa: BLE001
                pass
            await _autofix_render_success(
                baseline_name, program_name, cmp_res, state, validate_mode,
            )
            return

        remaining = cmp_res.get("failed_files", []) or []
        remaining_fail_count = (
            cmp_res.get("after_csv_fail", 0)
            + cmp_res.get("root_mixed_fail", 0)
        )
        iter_record["status"] = "kept_progress"
        iter_record["remaining_fail_count"] = remaining_fail_count
        state["iterations"].append(iter_record)
        state["applied"].append(iter_record)
        await cl.Message(content=(
            f"➡️ Iter {i}: compare now reports {remaining_fail_count} failures "
            f"across {len(remaining)} file(s). Keeping fix and continuing…"
        )).send()
        current_failed = remaining

    await _autofix_render_exhausted(
        baseline_name, program_name, state, validate_mode,
    )


# ─── Autonomous auto-fix action callbacks ─────────────────────────────

@cl.action_callback("autonomous_fix")
async def on_autonomous_fix(action: cl.Action):
    """🚀 Agent Auto-Fix button — kicks off the verify-before-suggest loop."""
    baseline_name = action.payload.get("baseline")
    failed_files = action.payload.get("failed_files") or []
    validate_mode = bool(action.payload.get("validate_mode", False))
    program_name = (
        action.payload.get("program_name")
        or _extract_program_name_safe(baseline_name)
    )
    await _autonomous_fix_loop(
        baseline_name=baseline_name,
        program_name=program_name,
        failed_files=failed_files,
        validate_mode=validate_mode,
    )


@cl.action_callback("autofix_revert_all")
async def on_autofix_revert_all(action: cl.Action):
    """Restore every file the autonomous loop touched to its pre-loop state."""
    from pathlib import Path as _P

    baseline_name = action.payload.get("baseline")
    state = _AUTOFIX_STATE.get(baseline_name) or {}
    snapshots = state.get("snapshots") or {}
    if not snapshots:
        await cl.Message(content=(
            "ℹ️ **Nothing to revert** — no autonomous-fix snapshots are recorded "
            "for this baseline. If you edited manually after the loop, use git."
        )).send()
        return

    reverted, failed = [], []
    for path, before in snapshots.items():
        try:
            _P(path).write_bytes(before)
            reverted.append(path)
        except OSError as exc:
            failed.append(f"{path}: {exc}")

    _AUTOFIX_STATE.pop(baseline_name, None)

    body = "\n".join([f"• `{p}`" for p in reverted]) if reverted else "(none)"
    err_body = (
        "\n\n**Failed to revert:**\n" + "\n".join([f"• {e}" for e in failed])
        if failed else ""
    )
    await cl.Message(content=(
        f"🗑️ **Reverted {len(reverted)} file(s)**\n\n{body}{err_body}\n\n"
        f"Re-run `compare {baseline_name}` to confirm the pre-loop state."
    )).send()


@cl.action_callback("autofix_finalize_now")
async def on_autofix_finalize_now(action: cl.Action):
    """Agent verified the fix — jump straight to the finalize step."""
    baseline_name = action.payload.get("baseline")
    _AUTOFIX_STATE.pop(baseline_name, None)
    await handle_finalize(baseline_name)


@cl.action_callback("autofix_resume_validate")
async def on_autofix_resume_validate(action: cl.Action):
    """Agent verified inside validate_mode — resume the composite at await_push."""
    from agents.validation_agent.validate_agent import resume_after_acceptable

    baseline_name = action.payload.get("baseline")
    _AUTOFIX_STATE.pop(baseline_name, None)
    skill = load_validate_skill()
    session = session_manager.get_session(baseline_name)
    await _emit_pipeline_event("resume", baseline=baseline_name)
    await _emit_pipeline_event("step_done", num=6, op="compare")
    await _consume_validate_events(
        resume_after_acceptable(session), baseline_name, skill,
        total=skill.get("total_steps", 7),
    )


@cl.action_callback("autofix_manual_mode")
async def on_autofix_manual_mode(action: cl.Action):
    """Loop exhausted — hand off to the manual Analyse & Fix card."""
    baseline_name = action.payload.get("baseline")
    program_name = (
        action.payload.get("program_name")
        or _extract_program_name_safe(baseline_name)
    )
    validate_mode = bool(action.payload.get("validate_mode", False))

    session = session_manager.get_session(baseline_name)
    cmp_data = session.get_step_data("step_6_compare") or {}
    failed_files = cmp_data.get("failed_files") or []
    if not failed_files:
        await cl.Message(content=(
            "ℹ️ No failing files recorded on the session. Re-run "
            f"`compare {baseline_name}` first."
        )).send()
        return
    await _render_compare_analysis_card(
        baseline_name=baseline_name,
        program_name=program_name,
        failed_files=failed_files,
        validate_mode=validate_mode,
    )


@cl.action_callback("autofix_accept_remaining")
async def on_autofix_accept_remaining(action: cl.Action):
    """Accept whatever diffs remain after an exhausted autonomous run."""
    baseline_name = action.payload.get("baseline")
    validate_mode = bool(action.payload.get("validate_mode", False))
    _AUTOFIX_STATE.pop(baseline_name, None)

    if validate_mode:
        from agents.validation_agent.validate_agent import resume_after_acceptable
        skill = load_validate_skill()
        session = session_manager.get_session(baseline_name)
        await cl.Message(content=(
            "✅ Remaining differences accepted. Resuming validate workflow…"
        )).send()
        await _consume_validate_events(
            resume_after_acceptable(session), baseline_name, skill,
            total=skill.get("total_steps", 7),
        )
    else:
        from prompts.skills.compare import mark_acceptable
        session = session_manager.get_session(baseline_name)
        mark_acceptable(session)
        await cl.Message(content=(
            f"✅ Remaining differences marked as **acceptable**.\n\n"
            f"**Next:** Run `finalize {baseline_name}`."
        )).send()


@cl.action_callback("retry_analysis_compare")
async def on_retry_analysis_compare(action: cl.Action):
    """🔄 Re-analyse on the compare-diff card — fresh suggestion for same file."""
    baseline_name = action.payload.get("baseline")
    failed_files = action.payload.get("failed_files") or []
    validate_mode = bool(action.payload.get("validate_mode", False))
    program_name = (
        action.payload.get("program_name")
        or _extract_program_name_safe(baseline_name)
    )

    analysis_agent = get_analysis_agent()
    sess = analysis_agent.get_session(baseline_name)
    if sess and not sess.can_retry:
        await cl.Message(content=(
            f"⚠️ **Maximum Attempts Reached**\n\n"
            f"**Attempts:** {sess.current_attempt}/{sess.max_attempts}\n\n"
            f"The AI has tried its budget on this baseline. Review the "
            f"previous suggestions, fix manually, or mark the remaining "
            f"differences as acceptable."
        )).send()
        return

    await _render_compare_analysis_card(
        baseline_name=baseline_name,
        program_name=program_name,
        failed_files=failed_files,
        validate_mode=validate_mode,
    )



@cl.action_callback("validate_acceptable")
async def on_validate_acceptable(action: cl.Action):
    """User clicked ✅ Acceptable on the compare-differences dialog."""
    from agents.validation_agent.validate_agent import resume_after_acceptable

    baseline_name = action.payload.get("baseline")
    report_path = action.payload.get("report_path", "")

    skill = load_validate_skill()
    cmp_cfg = skill.get("compare", {})
    await cl.Message(content=cmp_cfg.get(
        "acceptable_ack", "✅ Differences marked as acceptable."
    )).send()

    await _emit_pipeline_event("resume", baseline=baseline_name)
    await _emit_pipeline_event("step_done", num=6, op="compare")

    session = session_manager.get_session(baseline_name)
    await _consume_validate_events(
        resume_after_acceptable(session), baseline_name, skill,
        total=skill.get("total_steps", 7),
        initial_report_path=report_path,
    )


@cl.action_callback("validate_not_acceptable")
async def on_validate_not_acceptable(action: cl.Action):
    """User clicked ❌ Not Acceptable — stop the workflow."""
    baseline_name = action.payload.get("baseline")
    report_path = action.payload.get("report_path", "")

    await _emit_pipeline_event("dismiss")

    skill = load_validate_skill()
    cmp_cfg = skill.get("compare", {})
    not_ok_tpl = (
        cmp_cfg.get("not_acceptable")
        or "❌ Validation stopped."
    ).strip()
    await cl.Message(content=not_ok_tpl.format(
        baseline_name=baseline_name,
        report_path=report_path or "N/A",
    )).send()


@cl.action_callback("validate_approve_push")
async def on_validate_approve_push(action: cl.Action):
    """User clicked 🚀 Proceed to Push — run finalize push stage."""
    from agents.validation_agent.validate_agent import resume_after_push_approval

    baseline_name = action.payload.get("baseline")
    report_path = action.payload.get("report_path", "")

    skill = load_validate_skill()
    session = session_manager.get_session(baseline_name)

    await _emit_pipeline_event("resume", baseline=baseline_name)

    await _consume_validate_events(
        resume_after_push_approval(session), baseline_name, skill,
        total=skill.get("total_steps", 7),
        initial_report_path=report_path,
    )


@cl.action_callback("validate_approve_pr")
async def on_validate_approve_pr(action: cl.Action):
    """User clicked 📥 Raise PR & Close Ticket — run finalize PR stage."""
    from agents.validation_agent.validate_agent import resume_after_pr_approval

    baseline_name = action.payload.get("baseline")

    skill = load_validate_skill()
    session = session_manager.get_session(baseline_name)

    await _emit_pipeline_event("resume", baseline=baseline_name)

    await _consume_validate_events(
        resume_after_pr_approval(session), baseline_name, skill,
        total=skill.get("total_steps", 7),
    )


async def handle_reporting(baseline_name: str):
    """Handle the reporting command - pure UI layer.

    Step sequencing, branching, and close-ticket live in the
    composite agent at agents.validation_agent.reporting_agent. This
    function consumes the events that agent yields and renders them
    into Chainlit UI using templates from prompts/REPORTING_SKILL.md.
    """
    if not baseline_name:
        await cl.Message(content=_msg("usage.reporting")).send()
        return

    from agents.validation_agent.reporting_agent import run as run_reporting, STEPS as REPORTING_STEPS

    skill = load_reporting_skill()
    session = session_manager.get_session(baseline_name)
    total = skill.get("total_steps", 6)

    start_msg = skill.get("start_message", "").strip()
    if start_msg:
        await cl.Message(content=start_msg.format(baseline_name=baseline_name)).send()

    # Fire up the floating pipeline HUD. Step titles are read from the
    # composite agent's STEPS tuple (single source of truth) + step 6.
    step_titles = ", ".join([t for _, _, t in REPORTING_STEPS] + ["Close Ticket"])
    step_ops    = ", ".join([o for _, o, _ in REPORTING_STEPS] + ["close"])
    await _emit_pipeline_event(
        "start",
        baseline=baseline_name,
        steps=step_titles,
        ops=step_ops,
    )

    await _consume_reporting_events(run_reporting(session), baseline_name, skill, total)


async def _consume_reporting_events(
    event_stream,
    baseline_name: str,
    skill: dict,
    total: int,
    initial_report_path: str = "",
):

    """Render Chainlit UI in response to events from the reporting agent.

    Shared consumer for both entry points into the composite agent
    (`run` for a fresh workflow, `resume_after_acceptable` for the
    Acceptable-button path). Keeping render logic here means the
    reporting agent stays UI-free and unit-testable, while every
    on-screen string still comes from prompts/REPORTING_SKILL.md
    YAML templates - same as before the refactor.
    """
    steps_by_op = {s["id"]: s for s in skill.get("steps", [])}
    cmp = skill.get("compare", {})
    close_cfg = skill.get("close_ticket", {})
    report_path = initial_report_path

    async for event in event_stream:
        phase = event["phase"]

        if phase == "step":
            num = event["num"]
            op = event["op"]
            title = event["title"]
            task = event["task"]

            # Notify the floating pipeline HUD that this step is now
            # active (it will start pulsing).
            await _emit_pipeline_event("step_start", num=num, op=op)

            # Pick the YAML config block for THIS step.
            if op == "compare":
                cfg = cmp
            elif op == "close":
                cfg = close_cfg
            else:
                cfg = steps_by_op.get(op, {})

            # NOTE: the YAML `pre_message` field is intentionally
            # NOT rendered here. The ProgressCard title below
            # (`Step N/6: <title>`) already announces the step, so
            # sending pre_message would print the same thing twice.
            # Steps 1/2 don't have pre_message and looked cleanest;
            # dropping it for 3/4/5/6 gives every step the same UX.

            async with ProgressCard(
                f"Step {num}/{total}: {title}", baseline_name,
                interval=cfg.get("progress_interval", 10),
                eta_seconds=cfg.get("progress_eta", 120),
            ) as card:
                await card.wait_for(
                    task, status=cfg.get("progress_status", "Processing...")
                )
                ok, result = await task
                if isinstance(result, str):
                    result = {"message": result}
                if ok:
                    await card.complete(
                        cfg.get("progress_complete", f"{title} complete")
                    )
                else:
                    error = _extract_step_error(result, cfg.get("error_path"))
                    await card.fail(
                        error or cfg.get("progress_fail_default", "Failed")
                    )

            if not ok:
                error = (
                    _extract_step_error(result, cfg.get("error_path"))
                    or "Unknown error"
                )
                fail_tpl = cfg.get("fail", "❌ Step failed: {error}")
                # Pass every field a fail template might reference. The
                # close_ticket fail template references baseline_name and
                # report_path in addition to error — missing any raises
                # KeyError. Extra kwargs are ignored by templates that
                # don't use them, so it's safe to always pass all three.
                await cl.Message(content=fail_tpl.format(
                    error=error,
                    baseline_name=baseline_name,
                    report_path=report_path or "N/A",
                )).send()
                # Notify pipeline HUD of the failure (step turns red).
                await _emit_pipeline_event("step_fail", num=num, op=op)
                return

            # Compare success is rendered by all_pass/differences event;
            # close success is rendered by close_done event. Steps 1-4
            # render the per-step success template here.
            if op == "compare":
                report_path = result.get("report_path", "")
                # Compare-step done state is signaled by all_pass or
                # differences event below (not here).
                continue
            if op == "close":
                # Close-step done state is signaled by close_done below.
                continue

            # Steps 1-4 success — mark done in the pipeline HUD.
            await _emit_pipeline_event("step_done", num=num, op=op)

            success_tpl = cfg.get("success", "")
            if success_tpl:
                try:
                    safe = {
                        k: (v if v is not None else "")
                        for k, v in result.items()
                        if isinstance(k, str)
                    }
                    await cl.Message(content=success_tpl.format(**safe)).send()
                except (KeyError, IndexError, ValueError):
                    await cl.Message(content=success_tpl).send()

        elif phase == "all_pass":
            result = event["result"]
            # Compare passed — mark step 5 done in the pipeline HUD.
            await _emit_pipeline_event("step_done", num=5, op="compare")
            all_pass_tpl = cmp.get("all_pass", "✅ **Step 5: Compare - All Pass!**")
            await cl.Message(content=all_pass_tpl.format(
                csv_pass=result.get("after_csv_pass", 0),
                csv_fail=result.get("after_csv_fail", 0),
                root_pass=result.get("root_mixed_pass", 0),
                root_fail=result.get("root_mixed_fail", 0),
            ).strip()).send()

        elif phase == "differences":
            result = event["result"]
            report_path = result.get("report_path", "")
            # Compare paused for user decision — amber pulse in the HUD.
            await _emit_pipeline_event("step_pause", num=5, op="compare")
            csv_pass = result.get("after_csv_pass", 0)
            csv_fail = result.get("after_csv_fail", 0)
            root_pass = result.get("root_mixed_pass", 0)
            root_fail = result.get("root_mixed_fail", 0)
            failed_files = result.get("failed_files", [])
            failed_list = (
                "\n".join([f"• {f}" for f in failed_files])
                if failed_files else "• See report"
            )
            csv_icon = "✅" if csv_fail == 0 else "⚠️"
            root_icon = "✅" if root_fail == 0 else "⚠️"

            diff_tpl = cmp.get(
                "differences_found", "⚠️ **Step 5: Differences Found**"
            )
            content = diff_tpl.format(
                csv_pass=csv_pass, csv_fail=csv_fail,
                root_pass=root_pass, root_fail=root_fail,
                csv_icon=csv_icon, root_icon=root_icon,
                failed_list=failed_list, report_path=report_path,
            ).strip()

            actions = [
                cl.Action(
                    name="reporting_acceptable",
                    payload={"baseline": baseline_name, "report_path": report_path},
                    label=cmp.get("acceptable_label", "✅ Acceptable"),
                ),
                cl.Action(
                    name="reporting_not_acceptable",
                    payload={"baseline": baseline_name, "report_path": report_path},
                    label=cmp.get(
                        "not_acceptable_label",
                        "❌ Not Acceptable - Report to Validator",
                    ),
                ),
            ]
            await cl.Message(content=content, actions=actions).send()

        elif phase == "close_done":
            ok = event["ok"]
            result = event["result"]
            ticket_id = result.get("ticket_id")
            # Notify the pipeline HUD: step 6 done or failed, then
            # (on success) auto-dismiss the panel after a few seconds.
            await _emit_pipeline_event(
                "step_done" if ok else "step_fail",
                num=6, op="close",
            )
            if ok:
                await _emit_pipeline_event("complete")
            if ok:
                org = config.AZURE_DEVOPS_ORG
                project = config.AZURE_DEVOPS_PROJECT
                ticket_url = (
                    f"https://dev.azure.com/{org}/{project}"
                    f"/_workitems/edit/{ticket_id}"
                )
                success_tpl = close_cfg.get(
                    "success", "✅ Ticket {ticket_id} closed!"
                ).strip()
                await cl.Message(content=success_tpl.format(
                    ticket_id=ticket_id, ticket_url=ticket_url,
                    baseline_name=baseline_name, report_path=report_path,
                )).send()
            else:
                fail_tpl = close_cfg.get(
                    "fail", "⚠️ **Step 6: Close Ticket**\n\n{error}"
                ).strip()
                await cl.Message(content=fail_tpl.format(
                    error=result.get("message", "Could not close ticket"),
                    baseline_name=baseline_name, report_path=report_path,
                )).send()


@cl.action_callback("reporting_acceptable")
async def on_reporting_acceptable(action: cl.Action):
    """Handle the ✅ Acceptable button click in the reporting flow.

    Resumes the workflow at step 6 by feeding the shared event
    consumer with events from
    :func:`agents.validation_agent.reporting_agent.resume_after_acceptable`.
    """
    from agents.validation_agent.reporting_agent import resume_after_acceptable

    baseline_name = action.payload.get("baseline")
    report_path = action.payload.get("report_path", "")

    skill = load_reporting_skill()
    cmp = skill.get("compare", {})
    await cl.Message(
        content=cmp.get(
            "acceptable_ack", "✅ Differences marked as acceptable."
        )
    ).send()

    # Re-open the pipeline HUD (if user closed it) + mark compare as done
    # (user accepted the differences → treat as pass).
    await _emit_pipeline_event("resume", baseline=baseline_name)
    await _emit_pipeline_event("step_done", num=5, op="compare")

    session = session_manager.get_session(baseline_name)
    await _consume_reporting_events(
        resume_after_acceptable(session),
        baseline_name,
        skill,
        total=skill.get("total_steps", 6),
        initial_report_path=report_path,
    )


@cl.action_callback("reporting_not_acceptable")
async def on_reporting_not_acceptable(action: cl.Action):
    """Handle the ❌ Not Acceptable button click.

    Does NOT close the ticket. Displays the ticket-id (for the
    validator to pick up) and stops the flow. Uses the WIQL helper
    in the reporting skill folder directly — a read-only lookup
    doesn't need to go through the composite agent.
    """
    from prompts.skills.reporting.scripts.devops import find_reporting_ticket

    baseline_name = action.payload.get("baseline")
    report_path = action.payload.get("report_path", "")

    found, ticket_id, _ = find_reporting_ticket(baseline_name)
    ticket_info = f"**Ticket:** {ticket_id} (Not closed - pending fix)" if found else "**Ticket:** Not found"

    # Dismiss the floating HUD — this branch stops the workflow.
    await _emit_pipeline_event("dismiss")

    skill = load_reporting_skill()
    cmp = skill.get("compare", {})
    not_acceptable_tpl = cmp.get("not_acceptable", "❌ **Reporting Stopped - Issue Found**").strip()
    await cl.Message(content=not_acceptable_tpl.format(
        baseline_name=baseline_name, report_path=report_path,
        ticket_info=ticket_info,
    )).send()

def _extract_step_error(result: dict, error_path: str = None) -> str:
    """Extract error message from a reporting step result dict.

    If ``error_path`` is given (e.g. ``"error_info.error_message"``),
    traverses the nested dict first.  Always falls back to
    ``result["message"]``.
    """
    if error_path:
        node = result
        for part in error_path.split("."):
            if isinstance(node, dict):
                node = node.get(part)
            else:
                break
        if isinstance(node, str) and node:
            return node
    return result.get("message") or ""

# ─────────────────────────────────────────────────────────────────────────────
# Skill-driven handler registration
# ─────────────────────────────────────────────────────────────────────────────
# The orchestrator (services/orchestrator.py) reads
# prompts/WORKFLOW_ORCHESTRATION_SKILL.md and dispatches each user message to
# the handler registered here under the name in the skill's `handler:` field.
# Adding a new operation = add a skill entry + register a handler below.
# Existing handler functions are reused as-is — no behaviour change.
# ─────────────────────────────────────────────────────────────────────────────

register_many({
    "help":      handle_help,
    "list":      handle_list,
    "status":    handle_status,
    "reset":     handle_reset,
    "copy":      handle_copy,
    "load":      handle_load,
    "branch":    handle_branch,
    "run":       handle_run,
    "unload":    handle_unload,
    "compare":   handle_compare,
    "report":    handle_report,
    "finalize":  handle_finalize,
    "validate":  handle_validate,
    "reporting": handle_reporting,
    "chat":      handle_chat,   # natural-language fallback
})

# Eagerly initialise the orchestrator so any skill-file errors surface at
# startup rather than on the first message.
get_orchestrator()