---
id: run
command: run
aliases: []
description: "Run the migrated {TARGET_LANGUAGE_NAME} program (dotnet run)."
handler: run
entrypoint: ":run"
session_step: step_3b_run
on_failure_action: trigger_analysis
args:
  - name: baseline
    required: true
    validation: '^P\d{4,6}-[A-Z]{1,6}\d{2,8}[A-Z]?$'
    usage: "run <baseline>"
intent_patterns:
  - "run <baseline>"
  - "execute <baseline>"
  - "step 3b for <baseline>"
---

| | |
|---|---|
| **name** | run |
| **description** | Runs the migrated C# program via `dotnet run`, captures stdout/stderr, parses errors (SQL / build / runtime), and stores the full result on the session for downstream analysis. This is **Step 3b** of the validation workflow. |

# Run Skill — Step 3b

## When to use
Call this tool when the user asks to run / execute the migrated
target-language program, or "do step 3b". Examples:
- `run P02070-HPPL486P`
- "execute that baseline"
- "step 3b for HPPL486P"

## What it does
1. Extracts the program name from the baseline (`P02070-HPPL486P → HPPL486P`).
2. Executes `dotnet run -- <program>` inside the SSAB.OX project folder.
3. Parses stdout/stderr for SQL errors, build errors, and runtime
   exceptions (with stack traces) plus any C# warnings.
4. Stores the full result dict on ``session.run_result`` for
   downstream analysis (the ``analyse_with_ai`` AI-analysis button
   reads this after a failure).
5. Marks `step_3b_run` COMPLETED on success or FAILED with the
   parsed error message on failure.
6. On failure: the AnalysisAgent action button is rendered in
   ``app.py :: handle_run`` so the user can trigger root-cause
   analysis.

## Arguments
| Name | Required | Pattern | Example |
|------|----------|---------|---------|
| baseline | yes | `^P\d{4,6}-[A-Z]{1,6}\d{2,8}[A-Z]?$` | `P02070-HPPL486P` |

## Implementation

This skill is fully self-contained inside this folder. External
callers (`app.py`, the orchestrator, `reporting_agent.py`) must NOT
import any class, module, or tool from this folder except through:

```python
# Primary entrypoint (LLM-invoked "run X" command)
ok, result_dict = await orchestrator.run_skill("run", session)

# Pre-flight helpers used by handle_run in app.py
from prompts.skills.run import extract_program_name, find_program_file
program_name = extract_program_name(baseline)
found, path_or_msg = find_program_file(program_name)
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
prompts/skills/run/
├── SKILL.md                       ← this file (the contract)
├── __init__.py                    ← public surface:
│                                    - async run(session)
│                                    - extract_program_name(baseline)
│                                    - find_program_file(program_name)
└── scripts/
    └── run_baseline.py            ← merged implementation:
                                     • extract_program_name
                                     • find_program_file
                                     • _parse_error_output
                                     • _run_program (dotnet run subprocess)
                                     • run(session) orchestration
```

### Call chain

```
orchestrator.run_skill("run", session)
  └→ prompts/skills/run/__init__.py :: run(session)
        └→ prompts/skills/run/scripts/run_baseline.py :: run(session)
              └→ _run_program()  (dotnet run — Step 3b)
                    └→ _parse_error_output()  (regex parse of stdout/stderr)
```

### Contract

* **Entrypoint:** `:run` (function `run` in the package's `__init__.py`)
* **Signature:** `async def run(session: Session) -> Tuple[bool, Dict[str, Any]]`
* **Return dict keys:** `baseline`, `program`, `success`, `exit_code`,
  `error_info`, `output`, `program_path`
* **Side effects:**
  * mutates `session.step_3b_run` status (RUNNING → COMPLETED /
    FAILED with `program` + `exit_code` in the step data)
  * writes the full result dict onto `session.run_result` for the
    downstream AnalysisAgent to consume

### Helper functions (pre-flight)

* ``extract_program_name(baseline) -> str`` — pure string helper,
  `P02070-HPPL486P → HPPL486P`. Used by `handle_run` before
  dispatching so the UI can show the program name.
* ``find_program_file(program_name) -> Tuple[bool, str]`` — walks
  the SSAB.OX repo looking for `<program>.cs`. Used by `handle_run`
  as a pre-flight check so the UI can short-circuit with a friendly
  "Program Not Found" message instead of failing inside the skill.
