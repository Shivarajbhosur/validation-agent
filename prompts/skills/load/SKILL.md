---
id: load
command: load
aliases: [loadbaseline]
description: "Load baseline data into the database."
handler: load
entrypoint: ":run"
session_step: step_2_load
args:
  - name: baseline
    required: true
    validation: '^P\d{4,6}-[A-Z]{1,6}\d{2,8}[A-Z]?$'
    usage: "load <baseline>"
intent_patterns:
  - "load <baseline>"
  - "load data for <baseline>"
  - "step 2 for <baseline>"
---

| | |
|---|---|
| **name** | load |
| **description** | Runs the `load-BaselineData` PowerShell cmdlet for the baseline, marking **Step 2** of the validation workflow complete. |

# Load Skill — Step 2

## When to use
Call this tool when the user asks to load baseline data into the database,
or "do step 2". Examples:
- `load P02070-HPPL486P`
- "load data for that baseline"
- "step 2 for HPPL486P"

## What it does
1. Invokes the `load-BaselineData` PowerShell cmdlet for the baseline.
2. Marks `step_2_load` complete in the session.

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
ok, msg = await orchestrator.run_skill("load", session)
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
the PowerShell call and updates session state.

### Internal layout (single-file implementation)

```
prompts/skills/load/
├── SKILL.md                       ← this file (the contract)
├── __init__.py                    ← public surface: async run(session)
└── scripts/
    ├── __init__.py
    ├── _powershell.yaml           ← local cmdlet catalog (self-contained)
    └── load_baseline.py           ← merged implementation:
                                     • PowerShell cmdlet catalog reader
                                     • run(session) orchestration
                                     • UI response strings
```

### Call chain

```
orchestrator.run_skill("load", session)
  └→ prompts/skills/load/__init__.py :: run(session)
        └→ prompts/skills/load/scripts/load_baseline.py :: run(session)
              └→ load_baseline_data()   (PowerShell — Step 2)
```

### Contract

* **Entrypoint:** `:run` (function `run` in the package's `__init__.py`)
* **Signature:** `async def run(session: Session) -> Tuple[bool, str]`
* **Side effects:** mutates `session.step_2_load` status, invokes the
  `load-BaselineData` PowerShell cmdlet.
