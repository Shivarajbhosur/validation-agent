---
id: reporting
command: reporting
aliases: []
description: "Run the 6-step reporting workflow with stop-on-failure."
handler: reporting
args:
  - name: baseline
    required: true
    validation: '^P\d{4,6}-[A-Z]{1,6}\d{2,8}[A-Z]?$'
    usage: "reporting <baseline>"
intent_patterns:
  - "reporting <baseline>"
  - "run reporting on <baseline>"
---

| | |
|---|---|
| **name** | reporting |
| **description** | Runs the full 6-step reporting pipeline (copy → load → run → unload → compare → close-ticket) for a baseline, stopping on the first failure and closing the reporting ticket in Azure DevOps at the end. |

# Reporting Workflow — 6-step Pipeline

## When to use
Call this tool when the user asks to run the reporting workflow for a
baseline. Examples:
- `reporting P02070-HPPL486P`
- "run reporting on that baseline"

## What it does

Executes the 6-step reporting pipeline in strict order, delegating each
step to the corresponding standalone skill and stopping on the first
failure:

| # | Step | Delegated to |
|---|------|--------------|
| 1 | Copy Baseline         | `prompts/skills/copy/`    (via `run_skill("copy", session)`) |
| 2 | Load to DB            | `prompts/skills/load/`    (via `run_skill("load", session)`) |
| 3 | Run Program           | `prompts/skills/run/`     (via `run_skill("run", session)`) |
| 4 | Unload from DB        | `prompts/skills/unload/`  (via `run_skill("unload", session)`) |
| 5 | Compare Output        | `prompts/skills/compare/` (via `run_skill("compare", session)`) — if differences found, waits for user Acceptable/Not-Acceptable decision |
| 6 | Close Reporting Ticket | this folder's `scripts/devops.py :: close_reporting_ticket(session)` — finds `05-Reporting` ticket by baseline, PATCHes it to `Closed` |

## Architecture

Reporting is a **composite** workflow — it does not have a single
executable entrypoint like the standalone skills. There is no
`entrypoint:` field in this front-matter on purpose:

* The **router LLM** only uses this file for *discovery* — reading
  `id`, `handler`, `command`, `args`, and `intent_patterns` to route
  the user's `"reporting <baseline>"` message to the interactive
  handler.
* The **interactive handler** (`app.py :: handle_reporting`) drives
  the Chainlit UI (progress cards, per-step messages, Acceptable /
  Not-Acceptable action buttons, ticket-URL rendering).
* The **sequencing + branching logic** lives in a first-class
  composite agent at `agents/validation_agent/reporting_agent.py`,
  which yields events the interactive handler consumes to render UI.
* This folder's `scripts/devops.py` owns the Step-6 Azure DevOps
  HTTP (WIQL search + PATCH close) and is imported directly by both
  the composite agent (for step 6) and the `app.py` button callbacks
  (for the Not-Acceptable ticket-info display).

## Arguments

| Name | Required | Pattern | Example |
|------|----------|---------|---------|
| baseline | yes | `^P\d{4,6}-[A-Z]{1,6}\d{2,8}[A-Z]?$` | `P02070-HPPL486P` |

## File layout

```
prompts/skills/reporting/
├── SKILL.md                 ← this file (router metadata only)
└── scripts/
    └── devops.py            ← Step-6 helpers:
                                close_reporting_ticket(session)
                                find_reporting_ticket(baseline)
```

Callers **must** import directly from `scripts/devops.py`:

```python
from prompts.skills.reporting.scripts.devops import (
    close_reporting_ticket, find_reporting_ticket,
)
```
