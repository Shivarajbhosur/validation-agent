"""Shared sub-agent engine for the validation agent.

Provides one public helper, :func:`invoke_skill`, that every
``<op>_agent.py`` under this package calls to execute its skill's
entrypoint. This helper owns:

  1. Reading the skill's ``SKILL.md`` (YAML front-matter + body).
  2. The worker-LLM step (asks the LLM to parse the ``entrypoint:``
     field and choose which callable to invoke).
  3. ``importlib`` + ``getattr`` + ``await`` of the resolved
     callable inside ``prompts/skills/<op_id>/``.

Data flow is strictly unidirectional and sub-agents are
**decoupled from the orchestrator**:

    validation_agent.py  --->  <op>_agent.py  --->  invoke_skill()
                                                         |
                                                         v
                                                 prompts/skills/<op>/

Each sub-agent therefore reduces to:

    from agents.validation_agent.sub_agents import invoke_skill

    async def run(*args, **kwargs):
        return await invoke_skill("<op_id>", *args, **kwargs)

Behaviour, log lines, and the final invocation are identical to the
previous in-orchestrator path (same SKILL.md, same worker-LLM tool
schema, same fallback policy, same importlib resolution). The only
difference is where the code physically lives -- so the Chainlit UI
and existing call sites see no change.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml

import config
from utils.logger import get_logger

logger = get_logger("sub_agents")


# -------------------------------------------------------------------
# Paths & process-wide caches
# -------------------------------------------------------------------

# Convention: each operation's skill lives at
# ``prompts/skills/<op_id>/SKILL.md``. Same layout the orchestrator
# discovers at startup, so behaviour is unchanged.
_SKILLS_ROOT = Path(config.BASE_DIR) / "prompts" / "skills"

# Orchestration config (only the ``llm_skill_invocation`` flag is
# consumed here -- we read the same file as the orchestrator so the
# operator can toggle the worker LLM globally in one place).
_ORCH_CONFIG_PATH = _SKILLS_ROOT / "orchestration" / "_config.yaml"

# Parsed ``{op_id: {entrypoint, package, body}}`` -- read once per
# process and cached for hot paths.
_SKILL_CACHE: Dict[str, Dict[str, str]] = {}

# Cached ``llm_skill_invocation`` flag (None = not yet loaded).
_USE_LLM_CACHED: Optional[bool] = None


# -------------------------------------------------------------------
# Config & SKILL.md loaders (lazy + cached)
# -------------------------------------------------------------------


def _get_use_llm_default() -> bool:
    """Read ``agentic.llm_skill_invocation`` from the orchestration config.

    Cached after first read. Defaults to ``True`` if the file is
    missing or unreadable, matching the orchestrator's default.
    """
    global _USE_LLM_CACHED
    if _USE_LLM_CACHED is not None:
        return _USE_LLM_CACHED
    try:
        if _ORCH_CONFIG_PATH.exists():
            with _ORCH_CONFIG_PATH.open("r", encoding="utf-8") as fh:
                doc = yaml.safe_load(fh) or {}
            agentic = (doc.get("agentic") or {}) if isinstance(doc, dict) else {}
            _USE_LLM_CACHED = bool(agentic.get("llm_skill_invocation", True))
        else:
            _USE_LLM_CACHED = True
    except Exception as exc:  # noqa: BLE001 -- config read is best-effort
        logger.warning(
            f"Could not read llm_skill_invocation flag from "
            f"{_ORCH_CONFIG_PATH} ({exc}); defaulting to True"
        )
        _USE_LLM_CACHED = True
    return _USE_LLM_CACHED


def _load_skill_metadata(op_id: str) -> Dict[str, str]:
    """Parse and cache the skill's SKILL.md.

    Returns ``{entrypoint, package, body}`` where:
      * ``entrypoint`` is the raw ``<module>:<callable>`` string from
        the YAML front-matter.
      * ``package`` is the dotted Python package path of the skill
        folder (e.g. ``prompts.skills.copy``).
      * ``body`` is the Markdown after the front-matter -- passed to
        the worker LLM verbatim.
    """
    cached = _SKILL_CACHE.get(op_id)
    if cached is not None:
        return cached

    skill_path = _SKILLS_ROOT / op_id / "SKILL.md"
    if not skill_path.exists():
        raise FileNotFoundError(
            f"SKILL.md for op '{op_id}' not found at {skill_path}"
        )

    text = skill_path.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    if not match:
        raise ValueError(
            f"SKILL.md for op '{op_id}' is missing YAML front-matter"
        )
    front_matter = yaml.safe_load(match.group(1)) or {}
    body = match.group(2) or ""

    entrypoint = str(front_matter.get("entrypoint") or "").strip()
    if not entrypoint:
        raise ValueError(
            f"SKILL.md for op '{op_id}' is missing the 'entrypoint:' "
            f"field. Add e.g. 'entrypoint: \":run\"' to the YAML "
            f"front-matter so this skill can be invoked."
        )

    package = ".".join(
        skill_path.parent.relative_to(Path(config.BASE_DIR)).parts
    )

    info = {"entrypoint": entrypoint, "package": package, "body": body}
    _SKILL_CACHE[op_id] = info
    return info


# -------------------------------------------------------------------
# Worker-LLM step (decide which callable to invoke)
# -------------------------------------------------------------------


async def _llm_pick_entrypoint(
    op_id: str, package: str, body: str
) -> Optional[Tuple[str, str]]:
    """Ask the worker LLM to read SKILL.md and parse the entrypoint.

    The LLM is given the raw SKILL.md body and one generic tool,
    ``invoke_skill_entrypoint(module, function)``. It must read the
    YAML front-matter, find the ``entrypoint:`` field (format
    ``<module>:<function>``), and call the tool with the parsed
    values. Validates the choice resolves to a real callable inside
    ``package``; if not, returns ``None`` so the caller falls back
    to the declared entrypoint.
    """
    tool_schemas = [{
        "type": "function",
        "function": {
            "name": "invoke_skill_entrypoint",
            "description": (
                "Invoke the entrypoint declared in the YAML "
                "front-matter of SKILL.md. Read the 'entrypoint:' "
                "field which has the format '<module>:<function>', "
                "parse it, and pass the two parts as the 'module' "
                "and 'function' arguments. An empty <module> "
                "(e.g. ':run') means the callable lives in the "
                "package's __init__.py."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "module": {
                        "type": "string",
                        "description": (
                            "The sub-module path from BEFORE the "
                            "':' in the entrypoint field. Use an "
                            "empty string '' when the entrypoint "
                            "starts with ':' (callable lives in "
                            "__init__.py)."
                        ),
                    },
                    "function": {
                        "type": "string",
                        "description": (
                            "The callable name from AFTER the ':' "
                            "in the entrypoint field."
                        ),
                    },
                },
                "required": ["module", "function"],
            },
        },
    }]

    system_prompt = (
        f"You are the entrypoint resolver for the '{op_id}' skill.\n\n"
        "Below is the skill's SKILL.md (YAML front-matter + body). "
        "Your job:\n"
        "  1. Read the YAML front-matter at the top.\n"
        "  2. Find the 'entrypoint:' field.\n"
        "  3. The value has the format '<module>:<function>'.\n"
        "  4. Call the tool 'invoke_skill_entrypoint' with the two "
        "parsed parts as the 'module' and 'function' arguments.\n\n"
        "Examples:\n"
        "  entrypoint: \":run\"             -> module='', function='run'\n"
        "  entrypoint: \"agent:start\"      -> module='agent', function='start'\n"
        "  entrypoint: \"sub.mod:Handler\"  -> module='sub.mod', function='Handler'\n\n"
        "You MUST call the tool. Do not respond with plain text. "
        "You are not given the runtime arguments -- you are only "
        "telling the sub-agent which function declared in SKILL.md "
        "to invoke.\n\n"
        "# SKILL.md\n\n"
        f"{body}"
    )

    try:
        from services.llm_service import get_llm_service
        llm = get_llm_service()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"LLM service unavailable for skill invocation: {exc}")
        return None

    try:
        assistant_msg = await llm.chat_with_tools(
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        f"Resolve the entrypoint for the '{op_id}' "
                        "skill by reading the SKILL.md above and "
                        "calling invoke_skill_entrypoint with the "
                        "parsed values."
                    ),
                },
            ],
            tools=tool_schemas,
            tool_choice="required",
            max_tokens=256,
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"LLM skill invocation call failed for '{op_id}': {exc}")
        return None

    tool_calls = assistant_msg.get("tool_calls") or []
    if not tool_calls:
        logger.warning(
            f"LLM skill invocation: model did not emit a tool call "
            f"for '{op_id}' (response={assistant_msg.get('content')!r})"
        )
        return None

    fn = tool_calls[0].get("function") or {}
    if fn.get("name") != "invoke_skill_entrypoint":
        logger.warning(
            f"LLM skill invocation: model called unexpected tool "
            f"'{fn.get('name')}' for '{op_id}'"
        )
        return None

    raw_args = fn.get("arguments", "{}")
    try:
        parsed = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError as exc:
        logger.warning(
            f"LLM skill invocation: invalid tool args JSON for "
            f"'{op_id}': {exc} (raw={raw_args!r})"
        )
        return None

    llm_module = str(parsed.get("module") or "").strip()
    llm_function = str(parsed.get("function") or "").strip()
    if not llm_function:
        logger.warning(
            f"LLM skill invocation: model returned empty function "
            f"name for '{op_id}'"
        )
        return None

    if not _llm_choice_resolves(package, llm_module, llm_function):
        logger.warning(
            f"LLM skill invocation: '{llm_module}:{llm_function}' "
            f"does not resolve to a callable in package "
            f"'{package}' -- using declared entrypoint"
        )
        return None

    logger.info(
        f"LLM skill invocation: '{op_id}' -> "
        f"'{llm_module}:{llm_function}' (LLM read SKILL.md and "
        "parsed the entrypoint itself)"
    )
    return (llm_module, llm_function)


# -------------------------------------------------------------------
# importlib resolution helpers
# -------------------------------------------------------------------


def _ensure_base_dir_on_path() -> None:
    """Make sure ``config.BASE_DIR`` is on ``sys.path``. Idempotent."""
    base_dir = str(Path(config.BASE_DIR).resolve())
    if base_dir not in sys.path:
        sys.path.insert(0, base_dir)


def _build_full_module(package: str, module_part: str) -> str:
    """Combine ``package`` and the sub-module hint into a dotted path.

    An empty ``module_part`` (or ``":run"``-style entrypoint) targets
    the package's own ``__init__.py``.
    """
    mod = (module_part or "").strip()
    if not mod:
        if not package:
            raise ValueError(
                "Cannot resolve entrypoint: package is empty and "
                "no module hint was supplied."
            )
        return package
    if package and mod.startswith(package):
        return mod
    if package:
        return f"{package}.{mod.lstrip('.')}"
    return mod


def _import_or_load(package: str, module_part: str, full_module: str):
    """Import ``full_module`` (with file-based fallback).

    Fallback covers launcher conditions where ``sys.path`` has been
    trimmed and the dotted import would fail -- we load the module
    directly from its file path on disk.
    """
    try:
        return importlib.import_module(full_module)
    except ModuleNotFoundError:
        mod = (module_part or "").strip()
        if mod:
            rel_sub = mod.replace(".", "/") + ".py"
            file_path = (
                Path(config.BASE_DIR) / Path(*package.split(".")) / rel_sub
                if package
                else Path(config.BASE_DIR) / rel_sub
            )
        else:
            file_path = (
                Path(config.BASE_DIR)
                / Path(*package.split("."))
                / "__init__.py"
            )
        if not file_path.exists():
            raise
        spec = importlib.util.spec_from_file_location(full_module, file_path)
        if spec is None or spec.loader is None:
            raise
        module = importlib.util.module_from_spec(spec)
        sys.modules[full_module] = module
        spec.loader.exec_module(module)
        return module


def _llm_choice_resolves(
    package: str, module_part: str, attr_name: str
) -> bool:
    """Return True iff ``(module_part, attr_name)`` is a real callable.

    Safety boundary for the worker LLM: the LLM may pick *which*
    function to call, but the function must actually exist inside
    the skill's package. Dry-run (target is resolved, not executed).
    """
    if not package:
        return False
    try:
        full_module = _build_full_module(package, module_part)
    except ValueError:
        return False

    _ensure_base_dir_on_path()
    try:
        module = _import_or_load(package, module_part, full_module)
    except Exception:  # noqa: BLE001
        return False

    try:
        target: Any = module
        for part in attr_name.split("."):
            target = getattr(target, part)
    except AttributeError:
        return False
    return callable(target)


async def _invoke_callable(
    package: str,
    module_part: str,
    attr_name: str,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Resolve ``attr_name`` inside ``package`` and call it.

    Awaits the result if it is a coroutine; otherwise returns it
    as-is. ``*args`` / ``**kwargs`` are forwarded verbatim.
    """
    full_module = _build_full_module(package, module_part)
    _ensure_base_dir_on_path()
    module = _import_or_load(package, module_part, full_module)

    try:
        target: Any = module
        for part in attr_name.split("."):
            target = getattr(target, part)
    except AttributeError as exc:
        raise AttributeError(
            f"Module '{full_module}' has no attribute '{attr_name}': {exc}"
        ) from exc

    if not callable(target):
        raise TypeError(
            f"Attribute '{attr_name}' resolved to non-callable "
            f"{type(target).__name__}."
        )

    result = target(*args, **kwargs)
    if asyncio.iscoroutine(result):
        return await result
    return result


# -------------------------------------------------------------------
# Public sub-agent entrypoint
# -------------------------------------------------------------------


async def invoke_skill(op_id: str, *args: Any, **kwargs: Any) -> Any:
    """Run the skill at ``prompts/skills/<op_id>/`` via its entrypoint.

    Single shared helper every ``<op>_agent.py`` calls. Steps:

      1. Read & cache the skill's ``SKILL.md``.
      2. (Optional) ask the worker LLM to pick the callable. The
         flag ``agentic.llm_skill_invocation`` in
         ``prompts/skills/orchestration/_config.yaml`` controls
         whether the LLM is consulted. Any failure (LLM down,
         hallucinated callable, etc.) silently falls back to the
         declared entrypoint.
      3. ``importlib`` + ``getattr`` + ``await`` of the resolved
         callable. ``*args`` / ``**kwargs`` are forwarded verbatim
         to the skill entrypoint (typically ``(session,)``).

    The LLM only chooses *which* function to call -- runtime
    arguments are never sent to it, so non-serialisable objects
    like ``Session`` are safe.
    """
    info = _load_skill_metadata(op_id)
    entrypoint = info["entrypoint"]
    package = info["package"]
    body = info["body"]

    picked: Optional[Tuple[str, str]] = None
    if _get_use_llm_default():
        try:
            picked = await _llm_pick_entrypoint(op_id, package, body)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"LLM skill invocation failed for '{op_id}' "
                f"({exc}); falling back to declared entrypoint"
            )
            picked = None
    if picked is None:
        mod_part, _, attr_part = entrypoint.partition(":")
        picked = (mod_part.strip(), attr_part.strip())

    module_part, attr_name = picked
    if not attr_name:
        raise ValueError(
            f"Operation '{op_id}' has no callable attribute to "
            f"invoke (entrypoint={entrypoint!r})."
        )

    return await _invoke_callable(
        package, module_part, attr_name, *args, **kwargs
    )


__all__ = ["invoke_skill"]
