# Validation Agent — Regression Testing Framework

A self-contained, additive regression suite for the agentic validation
workflow. It validates **what the agents do** (step sequence), **what they
conclude** (exception category, root cause, KB article), **what they produce**
(validation report), and **how fast** (response-time thresholds) — on every
Pull Request.

The suite is **additive**: it adds files under `tests/` only and never changes
the application. External systems (the LLM gateway, PowerShell/.NET steps,
Azure DevOps) are mocked, and RAG retrieval runs against an isolated, offline
Chroma store, so the suite is deterministic and runs on any OS without
credentials or network.

## The eight agent steps

| # | Logical step | Real `Session` step key |
|---|--------------|-------------------------|
| 1 | Copy Baseline | `step_1_copy` |
| 2 | Load Data | `step_2_load` |
| 3 | Create Branch | `step_3a_branch` |
| 4 | Execute Program | `step_3b_run` |
| 5 | Analyze Exceptions | `step_4_analyze_fix` |
| 6 | Compare Outputs | `step_6_compare` |
| 7 | Generate Validation Report | `step_7_report` |
| 8 | Finalize Validation | `step_8_finalize` |

## What every regression run validates

1. **Correct agent sequence executed** — against the real
   `models.session.Session` state machine (a failed step halts the flow and the
   rest are marked `SKIPPED`).
2. **Correct exception category detected** — `run_failed` / `compare_diff` /
   `none`, aligned with `models.fix_suggestion.ErrorType`.
3. **Correct root cause identified** — required keywords present in the verdict.
4. **Correct knowledge base article retrieved** — top RAG hit matches the
   expected article id.
5. **Expected report generated** — status + required fields present.
6. **Response time under threshold** — per-case wall time ≤ configured limit.

## Layout

```
tests/
  conftest.py                 # fixtures (temp Session store) + HTML report hook
  regression/
    datasets/                 # one JSON file per regression case
    schema.py                 # dataset dataclasses + step mapping
    dataset_loader.py         # discover + validate datasets
    fake_llm.py               # deterministic LLM stub (mirrors LLMService)
    analysis.py               # 'Analyze Exceptions' step (parses LLM verdict)
    rag_harness.py            # offline, deterministic Chroma retrieval
    flow_executor.py          # runs the 8-step flow on the real Session
    flow_validator.py         # the six check functions
    report_builder.py         # builds the validation report
    html_report.py            # aggregate HTML report
    runner.py                 # runs a full case, aggregates checks
    test_regression.py        # parametrized entry point (1 test per dataset)
    report/                   # generated reports (git-ignored)
```

## Running locally

```powershell
# from the repo root, with the project venv active
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests/regression -v
```

The aggregate report is written to
`tests/regression/report/regression_report.html`.

## Adding a new regression case

Create a new JSON file under `datasets/`. The runner auto-discovers it and
emits one test per file. Schema:

```json
{
  "case_id": "REG-004-my-case",
  "description": "Short human-readable description.",
  "input": {
    "baseline_name": "P0XXXX-PROG",
    "program_name": "PROG",
    "error_message": "optional error text surfaced to the analysis step"
  },
  "step_outcomes": { "execute_program": "failed" },
  "llm_responses": {
    "analyze": {
      "exception_category": "run_failed",
      "root_cause": "Explanation containing the expected keywords.",
      "confidence": 0.9
    }
  },
  "kb_articles": [
    { "id": "KB-xxx", "title": "…", "content": "…", "metadata": {} }
  ],
  "kb_query": "query text used to retrieve the KB article",
  "expected": {
    "agent_sequence": ["copy_baseline", "load_data", "create_branch", "execute_program"],
    "exception_category": "run_failed",
    "root_cause_keywords": ["null reference", "customer master"],
    "kb_article_id": "KB-xxx",
    "report": {
      "status": "FAILED",
      "required_fields": ["baseline_name", "exception_category", "root_cause", "kb_reference", "steps"]
    },
    "max_response_time_ms": 15000
  }
}
```

Notes:
- `step_outcomes` maps a logical step to `"failed"` to simulate a failure there;
  steps after it are skipped. Omit for a clean pass.
- The analysis verdict is only produced when a step fails (matching the app).
- Set `kb_articles` + `kb_query` to exercise RAG; leave them empty for a clean
  pass (no KB lookup expected).
- **Turn every real bug into a new dataset case** so it can never regress.

## CI/CD

### Azure DevOps (this repo — `dev.azure.com/ExperSolve/...`)

The pipeline is defined in [`azure-pipelines.yml`](../../azure-pipelines.yml) at
the repo root. It installs a **slim CI dependency subset**
([`requirements-ci.txt`](../../requirements-ci.txt) — `python-dotenv`, `loguru`,
`chromadb` — plus `requirements-dev.txt`) rather than the full
`requirements.txt`, because the suite only imports `config`, `models.session`
and `chromadb`. This keeps runs fast and avoids disk-space pressure on hosted
agents, and does not change the application's own dependencies. It runs the
suite, publishes JUnit test results (visible on the **Tests** tab) and uploads
the HTML reports as a pipeline artifact.

**One-time setup:**

1. **Create the pipeline** — in Azure DevOps: *Pipelines → New pipeline →
   Azure Repos Git → select Validation-Agent → Existing Azure Pipelines YAML
   file → `/azure-pipelines.yml` → Save*. (This is what the **Set up build**
   button in Repos does.)
2. **CI trigger (push)** — already configured by the `trigger:` block. Every
   commit pushed to `main`, `Autopilot_Feature` or `feature/*` runs the
   pipeline automatically.
3. **PR trigger (pull request)** — Azure Repos ignores the YAML `pr:` key, so
   configure a **Build Validation branch policy** instead:
   *Repos → Branches → `main` → ⋯ → Branch policies → Build Validation → `+` →
   select this pipeline → Trigger: Automatic → Policy requirement: Required*.
   The pipeline then runs on every PR targeting `main` and **blocks merge** if
   it fails.

### GitHub Actions

[`.github/workflows/regression.yml`](../../.github/workflows/regression.yml)
provides the same suite for GitHub. It is **not used by Azure DevOps** — keep
it only if this repo is also mirrored to GitHub; otherwise it can be deleted.


## Extending toward real end-to-end

`flow_executor._simulate_step_work` is the single extension point. To drive the
real sub-agents, invoke each agent there with its subprocess/HTTP boundaries
mocked. True end-to-end runs (requiring `pwsh`, the .NET SDK and Beyond Compare)
belong under the `e2e` marker and are intentionally excluded from CI.
