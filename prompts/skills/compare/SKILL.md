---
id: compare
command: compare
aliases: []
description: "Compare generated output vs expected output (compare-x)."
handler: compare
entrypoint: ":run"
session_step: step_6_compare
args:
  - name: baseline
    required: true
    validation: '^P\d{4,6}-[A-Z]{1,6}\d{2,8}[A-Z]?$'
    usage: "compare <baseline>"
intent_patterns:
  - "compare <baseline>"
  - "diff output for <baseline>"
  - "step 6 for <baseline>"
---

| | |
|---|---|
| **name** | compare |
| **description** | Runs the `compare-x` PowerShell cmdlet to diff generated output vs expected output, parses the pass/fail summary, and writes the result into the session. This is **Step 6** of the validation workflow. |

# Compare Skill — Step 6

## When to use
Call this tool when the user asks to compare generated vs expected output
for a baseline, or "do step 6". Examples:
- `compare P02070-HPPL486P`
- "diff output for that baseline"
- "step 6 for HPPL486P"

## What it does
1. Invokes the `compare-x` PowerShell cmdlet (from the PowerShell library) to
   diff generated output vs expected output.
2. Parses `AFTER CSV` and `Root Mixed` pass/fail counters plus the list of
   failing files from the cmdlet's console output.
3. Writes the parsed summary into the session under `step_6_compare`.
4. If all files match → marks `step_6_compare` COMPLETED. If differences
   are found → keeps the step as RUNNING pending the user's Acceptable /
   Not Acceptable decision (surfaced as Chainlit action buttons in
   ``app.py :: handle_compare``).

A Word document popup may appear during the compare; the user must set
the security label and save the file manually if so.

## Arguments
| Name | Required | Pattern | Example |
|------|----------|---------|---------|
| baseline | yes | `^P\d{4,6}-[A-Z]{1,6}\d{2,8}[A-Z]?$` | `P02070-HPPL486P` |

## Implementation

This skill is fully self-contained inside this folder. External
callers (`app.py`, the orchestrator, the button callbacks in
`app.py`, `reporting_agent.py`) must NOT import any class, module,
or tool from this folder except through:

```python
# Primary entrypoint (LLM-invoked "compare X" command)
ok, result_dict = await orchestrator.run_skill("compare", session)

# Chainlit button callbacks (post-compare user decisions)
from prompts.skills.compare import mark_acceptable, mark_not_acceptable
ok, msg = mark_acceptable(session)
ok, msg = mark_not_acceptable(session)
```

The orchestrator, in turn, does **not** call `run()` directly. It
sends THIS SKILL.md to the LLM together with a tool schema exposing
the entrypoint declared above (`run`). The LLM reads this file,
understands what the skill does, and emits a `tool_call("run")`. So
the LLM is the one that decides "call `run`"; the orchestrator only
forwards the runtime arguments (e.g. `session`).

If the LLM call fails for any reason, the orchestrator automatically
falls back to invoking the declared entrypoint directly.

### Internal layout (single-file implementation)

```
prompts/skills/compare/
├── SKILL.md                       ← this file (the contract)
├── __init__.py                    ← public surface:
│                                    - async run(session)
│                                    - mark_acceptable(session)
│                                    - mark_not_acceptable(session)
└── scripts/
    └── compare_baseline.py        ← merged implementation:
                                     • PowerShell subprocess runner
                                       for the compare-x cmdlet
                                     • output-parsing regexes
                                     • run(session) orchestration
                                     • mark_acceptable / mark_not_acceptable
                                       session-state helpers
```

### Call chain

```
orchestrator.run_skill("compare", session)
  └→ prompts/skills/compare/__init__.py :: run(session)
        └→ prompts/skills/compare/scripts/compare_baseline.py :: run(session)
              └→ _run_compare()      (pwsh + compare-x cmdlet — Step 6)
                    └→ _parse_compare_output()   (regex parse of stdout)
```

### Contract

* **Entrypoint:** `:run` (function `run` in the package's `__init__.py`)
* **Signature:** `async def run(session: Session) -> Tuple[bool, Dict[str, Any]]`
* **Return dict keys:** `exit_code`, `after_csv_pass`, `after_csv_fail`,
  `root_mixed_pass`, `root_mixed_fail`, `all_pass`, `failed_files`,
  `report_path`, `output`, `message`
* **Side effects:** mutates `session.step_6_compare` status (RUNNING →
  COMPLETED / RUNNING pending user decision / FAILED); shells out to
  the `compare-x` PowerShell cmdlet.

### Helper functions (button callbacks)

* ``mark_acceptable(session) -> Tuple[bool, str]`` — user reviewed
  the differences and accepted them. Sets `step_6_compare` COMPLETED
  with `user_decision="acceptable"` in the step data.
* ``mark_not_acceptable(session) -> Tuple[bool, str]`` — user
  rejected the differences. Sets `step_6_compare` FAILED with
  `user_decision="not_acceptable"` in the step data.

These are NOT LLM-invoked ops — they are called only from the
Chainlit `@cl.action_callback` handlers in ``app.py`` when the user
clicks the corresponding button.
