---
id: finalize
command: finalize
aliases: []
description: "Commit, push feature branch, create PR to develop, link + close validation ticket."
handler: finalize
entrypoint: ":run"
session_step: step_8_finalize
args:
  - name: baseline
    required: true
    validation: '^P\d{4,6}-[A-Z]{1,6}\d{2,8}[A-Z]?$'
    usage: "finalize <baseline>"
intent_patterns:
  - "finalize <baseline>"
  - "step 8 for <baseline>"
  - "push and PR for <baseline>"
---

| | |
|---|---|
| **name** | finalize |
| **description** | Step 8 of the validation workflow: commits local changes on the current feature branch, pushes it to Azure DevOps, opens a Pull Request into `develop`, and closes the linked `03-Validation` ticket. |

# Finalize Skill — Step 8

## When to use
Call this tool after the user has completed steps 1-7 (copy → load →
branch → run → analyse → unload → compare → report) and wants to
deliver the baseline. Examples:
- `finalize P02070-HPPL486P`
- "push and open PR for P02070-HPPL486P"
- "do step 8 for that baseline"

## What it does

Runs **fail-fast** in this exact order:

| # | Sub-step | Action |
|---|---|---|
| 1 | Find validation ticket | WIQL search: title CONTAINS `<baseline>` AND CONTAINS `03-Validation`. Fail-fast if not found — nothing is committed / pushed. |
| 2 | Git add + commit | `git add .` then `git commit -m "Validation: <baseline>"`. "Nothing to commit" is treated as success (idempotent re-runs). |
| 3 | Git push | Push current branch to Azure DevOps. **SAFETY**: refuses to push to `master` / `main` / `develop`. |
| 4 | Create Pull Request | Opens PR from current feature branch → `develop` with title `<baseline> Validation #<ticket_id>`, description = commit message summary, and `workItemRefs: [ticket_id]` so the PR appears in the ticket's Development section. |
| 5 | Close validation ticket | PATCH `<ticket_id>` state → `Closed`, reason → `Completed`. `already closed` is treated as success. |

## Safety guarantees

* **Never pushes to protected branches** — if the current branch is
  `master`, `main`, or `develop`, sub-step 3 refuses and the whole
  workflow fails with a clear error before any HTTP calls.
* **Never auto-merges the PR** — the PR is opened as a *draft-review*
  for a human reviewer. Merge happens manually in Azure DevOps.
* **Fail-fast on missing validation ticket** — if no `03-Validation`
  ticket exists for the baseline, git is never touched.

## Arguments

| Name | Required | Pattern | Example |
|------|----------|---------|---------|
| baseline | yes | `^P\d{4,6}-[A-Z]{1,6}\d{2,8}[A-Z]?$` | `P02070-HPPL486P` |

## Architecture

Fully self-contained inside `prompts/skills/finalize/`. External
callers (`app.py :: handle_finalize`, the orchestrator) must NOT
import any class, module, or file from inside this folder except
through the facade's `run(session)` entrypoint.

```
prompts/skills/finalize/
├── SKILL.md                     ← this file (the contract)
├── __init__.py                  ← facade: exposes run(session)
└── scripts/
    ├── finalize_baseline.py     ← orchestrator (git + PR + close)
    ├── git_helpers.py           ← subprocess: get_current_branch,
    │                              is_protected_branch, add, commit, push
    └── devops.py                ← Azure DevOps HTTP:
                                    find_validation_ticket_by_baseline
                                    create_pull_request
                                    close_validation_ticket
```

## Contract

* **Entrypoint:** `:run` (function `run` in the package's `__init__.py`)
* **Signature:** `async def run(session: Session) -> Tuple[bool, Dict[str, Any]]`
* **Session step:** `step_8_finalize` (RUNNING → COMPLETED with
  `branch_name`, `commit_message`, `pr_id`, `pr_url`, `ticket_id` in
  data, or FAILED with error message).
* **Result dict keys:** `baseline`, `branch_name`, `commit_success`,
  `push_success`, `pr_id`, `pr_url`, `ticket_id`, `ticket_closed`,
  `message`.
