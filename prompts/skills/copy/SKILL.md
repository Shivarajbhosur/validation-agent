---
id: copy
command: copy
aliases: [copybaseline]
description: "Copy baseline test files from G: drive to C: working directory."
handler: copy
entrypoint: ":run"
session_step: step_1_copy
args:
  - name: baseline
    required: true
    validation: '^P\d{4,6}-[A-Z]{1,6}\d{2,8}[A-Z]?$'
    usage: "copy <baseline>"
intent_patterns:
  - "copy <baseline>"
  - "copy files for <baseline>"
  - "step 1 for <baseline>"
---

| | |
|---|---|
| **name** | copy |
| **description** | Copies baseline test files from `G:` to `C:` and prepares SYSOUT, `.env`, and input files. This is **Step 1** of the validation workflow. |

# Copy Skill — Step 1

## When to use
Call this tool when the user asks to copy baseline files, prepare the
working folder, or "do step 1". Examples:
- `copy P02070-HPPL486P`
- "copy files for that baseline"
- "do step 1 for HPPL486P"

## What it does
1. Copies the baseline folder from G: drive to C: working directory.
2. Creates SYSOUT via `New-Sysout`.
3. Writes per-baseline `.env` via `New-Env`.
4. Copies input files via `Copy-Input`.
5. Marks `step_1_copy` complete in the session.

## Arguments
| Name | Required | Pattern | Example |
|------|----------|---------|---------|
| baseline | yes | `^P\d{4,6}-[A-Z]{1,6}\d{2,8}[A-Z]?$` | `P02070-HPPL486P` |

## Implementation

This skill is fully self-contained inside this folder. External
callers (`app.py`, the orchestrator, `script_runner_agent.py`) must
NOT import any class, module, or tool from this folder. They invoke
the skill through a single, generic entry point:

```python
ok, msg = await orchestrator.run_skill("copy", session)
```

The orchestrator, in turn, does **not** call `run()` directly.
Instead it sends THIS SKILL.md (the markdown you are reading) to the
LLM together with a tool schema exposing the entrypoint declared
above (`run`). The LLM reads this file, understands what the skill
does, and emits a `tool_call("run")`. The orchestrator then invokes
the picked function. So the LLM is the one that decides "call `run`";
the orchestrator only forwards the runtime arguments (e.g. `session`).

If the LLM call fails for any reason, the orchestrator automatically
falls back to invoking the declared entrypoint directly so the
operation never silently stalls. The flag
`agentic.llm_skill_invocation` in
`prompts/skills/orchestration/_config.yaml` controls this (default
`true`).

Inside this folder nothing changes whichever path is taken: `run()`
in `__init__.py` delegates to a single merged module that performs
the four sub-steps in order.

### Internal layout (single-file implementation)

```
prompts/skills/copy/
├── SKILL.md                       ← this file (the contract)
├── __init__.py                    ← public surface: async run(session)
└── scripts/
    ├── __init__.py
    ├── _powershell.yaml           ← local cmdlet catalog (self-contained)
    └── copy_baseline.py           ← merged implementation:
                                     • filesystem helpers
                                     • PowerShell cmdlets
                                     • run(session) orchestration
                                     • UI response strings
```

### Call chain

```
orchestrator.run_skill("copy", session)
  └→ prompts/skills/copy/__init__.py :: run(session)
        └→ prompts/skills/copy/scripts/copy_baseline.py :: run(session)
              ├→ copy_baseline()    (shutil — Step 1a)
              ├→ new_sysout()       (PowerShell — Step 1b)
              ├→ new_env()          (PowerShell — Step 1c)
              └→ copy_input()       (PowerShell — Step 1d)
```

### Contract

* **Entrypoint:** `:run` (function `run` in the package's `__init__.py`)
* **Signature:** `async def run(session: Session) -> Tuple[bool, str]`
* **Side effects:** mutates `session.step_1_copy` status, writes files
  to `C:`, invokes the three PowerShell cmdlets above.

