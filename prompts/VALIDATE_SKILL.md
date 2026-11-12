# Validation Workflow Skill

Defines the 7-step full validation workflow executed by `handle_validate`
in `app.py`. Step definitions, progress-card settings, and user-facing
messages live here so they can be edited without touching Python code.

Actual execution stays in `agents/validation_agent/validate_agent.py` —
the skill file is configuration only.

The workflow pauses at 3 points:
1. **Run failure** — user gets the existing "Analyse with AI" button
2. **Compare differences** — user accepts or rejects
3. **Before Push** — user approves push
4. **Before PR** — user approves raising PR + closing ticket

```yaml
validate_workflow:
  total_steps: 7

  start_message: |
    🚀 **Validation Task Started**

    **Baseline:** `{baseline_name}`

    Running 7 steps automatically...

  steps:
    - id: copy
      number: 1
      title: Copy Baseline
      progress_interval: 10
      progress_eta: 120
      progress_status: "Copying input files..."
      progress_complete: "Copy complete"
      progress_fail_default: "Copy failed"
      success: "✅ **Step 1: Copy complete!**\n\n    {message}"
      fail: "❌ **Step 1 Failed: Copy**\n\n{error}\n\n**Action:** Fix the issue and re-run `validate {baseline_name}`."

    - id: load
      number: 2
      title: Load to DB
      progress_interval: 10
      progress_eta: 90
      progress_status: "Loading baseline data..."
      progress_complete: "Load complete"
      progress_fail_default: "Load failed"
      success: "✅ Step 2: Load complete!"
      fail: "❌ **Step 2 Failed: Load**\n\n{error}\n\n**Action:** Fix the issue and re-run `validate {baseline_name}`."

    - id: branch
      number: 3
      title: Create Branch
      progress_interval: 10
      progress_eta: 30
      progress_status: "Creating feature branch..."
      progress_complete: "Branch created"
      progress_fail_default: "Branch creation failed"
      success: "✅ Step 3: Branch created!"
      fail: "❌ **Step 3 Failed: Branch**\n\n{error}\n\n**Action:** Fix the issue and re-run `validate {baseline_name}`."

    - id: run
      number: 4
      title: Run Program
      progress_interval: 15
      progress_eta: 180
      progress_status: "Executing program..."
      progress_complete: "Run complete"
      progress_fail_default: "Run failed"
      error_path: "error_info.error_message"
      success: "✅ Step 4: Run complete!"
      # Note: `fail` is intentionally NOT set for the run step — a run
      # failure yields a `run_fail` event which is rendered by the
      # dedicated `run_fail` block below (with the Analyse-with-AI +
      # Fix-Applied buttons).

    - id: unload
      number: 5
      title: Unload from DB
      progress_interval: 10
      progress_eta: 90
      progress_status: "Unloading baseline data..."
      progress_complete: "Unload complete"
      progress_fail_default: "Unload failed"
      success: "✅ Step 5: Unload complete!"
      fail: "❌ **Step 5 Failed: Unload**\n\n{error}\n\n**Action:** Fix the issue and re-run `validate {baseline_name}`."

  # ── Run failure pause block ─────────────────────────────────────
  # Rendered when step 4 (run) fails. The consumer emits the same
  # rich error display as the standalone `run` command and shows
  # the existing "🤖 Analyse with AI" button (payload carries
  # `validate_mode=True` so `on_mark_fix_applied` continues the
  # composite instead of showing manual next-steps).
  run_fail:
    header: |
      ❌ **Step 4: Run Failed**

      Program `{program_name}` for `{baseline_name}` failed.

    guidance: |

      **Next Steps:**
      • Click **🤖 Analyse with AI** to get an automated diagnosis + fix suggestion
      • After applying the fix in your code, click **✅ Fix Applied - Run Again** to retry
      • If the retry succeeds, the workflow will continue automatically with Unload → Compare → …

  # ── Compare step ────────────────────────────────────────────────
  compare:
    pre_message: "🔄 **Step 6/7: Compare Output...**\n\n📄 **Note:** A Word document popup may appear. If it does, please set the security label and save it manually."
    progress_interval: 15
    progress_eta: 180
    progress_status: "Comparing output & generating report..."
    progress_complete: "Compare complete"
    progress_fail_default: "Compare failed"
    fail: "❌ **Step 6 Failed: Compare**\n\n{error}\n\n**Action:** Fix the issue and re-run `validate {baseline_name}`."
    all_pass: |
      ✅ **Step 6: Compare - All Pass!**

      **Results:**
      ├── AFTER CSV: {csv_pass} pass, {csv_fail} fail ✅
      └── Root Mixed: {root_pass} pass, {root_fail} fail ✅
    differences_found: |
      ⚠️ **Step 6: Differences Found**

      **Results:**
      ├── AFTER CSV: {csv_pass} pass, {csv_fail} fail {csv_icon}
      └── Root Mixed: {root_pass} pass, {root_fail} fail {root_icon}

      **Differences in:**
      {failed_list}

      **Report:** `{report_path}`

      Please review and confirm:
    acceptable_label: "✅ Acceptable"
    not_acceptable_label: "❌ Not Acceptable"
    acceptable_ack: "✅ Differences marked as acceptable. Preparing to push..."
    not_acceptable: |
      ❌ **Validation Stopped - Differences Not Acceptable**

      **Baseline:** `{baseline_name}`

      **Action Required:**
      1. Review report at: `{report_path}`
      2. Fix the code
      3. Re-run `validate {baseline_name}` when ready

  # ── Await Push confirmation ─────────────────────────────────────
  await_push:
    message: |
      ✅ **Compare stage complete.**

      **Baseline:** `{baseline_name}`

      Ready to push your changes to Git.

      Click **🚀 Proceed to Push** below to continue, or dismiss this message to stop.
    button_label: "🚀 Proceed to Push"

  # ── Finalize · Push stage (7a) ──────────────────────────────────
  finalize_push:
    progress_interval: 10
    progress_eta: 60
    progress_status: "Committing & pushing..."
    progress_complete: "Push complete"
    progress_fail_default: "Push failed"
    success: |
      ✅ **Push complete!**

      **Branch:** `{branch_name}`

      {message}
    fail: |
      ❌ **Step 7a Failed: Push**

      {error}

      **Note:** No PR was raised. Please review the git state manually.

  # ── Await PR confirmation ───────────────────────────────────────
  await_pr:
    message: |
      ✅ **Branch pushed successfully.**

      **Branch:** `{branch_name}`
      **Baseline:** `{baseline_name}`

      Ready to raise the Pull Request and close the validation ticket.

      Click **📥 Raise PR & Close Ticket** below to continue.
    button_label: "📥 Raise PR & Close Ticket"

  # ── Finalize · PR stage (7b) ────────────────────────────────────
  finalize_pr:
    progress_interval: 10
    progress_eta: 45
    progress_status: "Raising PR & closing ticket..."
    progress_complete: "PR raised & ticket closed"
    progress_fail_default: "PR/close failed"
    success: |
      🎉 **Validation Complete!**

      **Baseline:** `{baseline_name}`
      **Branch:** `{branch_name}`
      **PR:** [{pr_id}]({pr_url})
      **Ticket:** [{ticket_id}]({ticket_url}) (Closed)

      All 7 steps completed successfully!
    fail: |
      ⚠️ **Step 7b: PR / Close Ticket**

      {error}

      **Note:** Branch was pushed successfully. Please raise the PR and close the ticket manually.

      **Baseline:** `{baseline_name}`
      **Branch:** `{branch_name}`
```
