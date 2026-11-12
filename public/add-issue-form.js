/* eslint-disable */
/**
 * /add-issue modal form
 *
 * Intercepts the user typing `/add-issue` in the chat input, shows a
 * proper full-screen modal form (title / program / category / tags /
 * root cause / buggy code / fixed code), and — on Save — serialises
 * the values back into the exact YAML-block template that the existing
 * server-side wizard parser (`_parse_add_issue_template` in app.py)
 * already understands. No server code changes required.
 *
 * The modal is styled with the same Ensono / Aurora palette used
 * elsewhere in public/aurora-premium.css and public/styles.css. All
 * DOM, styles and event listeners are scoped under the
 * ``#va-add-issue-modal`` root so nothing on the page leaks or
 * conflicts.
 */
(function () {
  if (window.__VA_ADD_ISSUE_BOOT__) return;
  window.__VA_ADD_ISSUE_BOOT__ = true;

  const MODAL_ID = "va-add-issue-modal";
  const CATEGORIES = [
    "wrapper_value_mismatch",
    "wrong_variable_name",
    "extra_function_wrapper",
    "missing_container_prefix",
    "format_conversion",
    "file_status_check",
    "other",
  ];

  // ── DOM helpers ────────────────────────────────────────────────
  function q(sel, root) { return (root || document).querySelector(sel); }

  function chatInput() {
    return (
      document.querySelector("#chat-input") ||
      document.querySelector("textarea[placeholder]") ||
      document.querySelector("textarea")
    );
  }

  function fillChatInputAndSubmit(text) {
    const input = chatInput();
    if (!input) {
      console.warn("[va-add-issue] chat input not found");
      return;
    }
    // Native setter so React state notices the change.
    const proto = Object.getPrototypeOf(input);
    const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
    setter && setter.call(input, text);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));

    // Prefer clicking the submit button; fall back to keyboard Enter.
    const submitBtn =
      document.querySelector("#chat-submit") ||
      document.querySelector("#message-composer button[type='submit']") ||
      document.querySelector("#message-composer button:has(svg.lucide-arrow-up)");
    if (submitBtn && !submitBtn.disabled) {
      submitBtn.click();
      return;
    }
    input.focus();
    input.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Enter", code: "Enter", bubbles: true }),
    );
  }

  function serializeToTemplate(values) {
    // Matches the exact block the server parser (_parse_add_issue_template)
    // consumes: YAML front-matter for single-line fields, then dedicated
    // ===== <FIELD> ===== blocks for multi-line ones (symptoms, root_cause,
    // buggy_code, fixed_code). Fenced code blocks are optional inside the
    // multi-line blocks; the parser strips them.
    const yaml =
      "```yaml\n" +
      `title:            ${values.title || ""}\n` +
      `program:          ${values.program || ""}\n` +
      `error_signature:  ${values.error_signature || ""}\n` +
      `bug_category:     ${values.bug_category || "other"}\n` +
      `tags:             ${values.tags || ""}\n` +
      "```";
    const symptoms = values.symptoms
      ? "===== SYMPTOMS =====\n" + values.symptoms.trimEnd() + "\n"
      : "";
    const rootCause = values.root_cause
      ? "===== ROOT CAUSE =====\n" + values.root_cause.trimEnd() + "\n"
      : "";
    const buggy =
      "===== BUGGY CODE =====\n" +
      "```csharp\n" +
      (values.buggy_code || "").trimEnd() +
      "\n```";
    const fixed =
      "===== FIXED CODE =====\n" +
      "```csharp\n" +
      (values.fixed_code || "").trimEnd() +
      "\n```";
    return [yaml, symptoms, rootCause, buggy, fixed]
      .filter(Boolean)
      .join("\n\n") + "\n";
  }

  // ── One-off style injection ─────────────────────────────────────
  function injectStyles() {
    if (document.getElementById("va-add-issue-styles")) return;
    const css = `
      #${MODAL_ID}[hidden]{display:none!important}
      #${MODAL_ID}{
        position:fixed;inset:0;z-index:2147483000;
        display:flex;align-items:center;justify-content:center;
        background:radial-gradient(ellipse at 30% 20%,rgba(108,47,206,.28),transparent 55%),
                   radial-gradient(ellipse at 70% 80%,rgba(139,92,246,.24),transparent 55%),
                   rgba(6,4,18,.72);
        backdrop-filter:blur(14px) saturate(140%);
        -webkit-backdrop-filter:blur(14px) saturate(140%);
        animation:vaAddIssueFadeIn .18s ease-out;
      }
      @keyframes vaAddIssueFadeIn{from{opacity:0}to{opacity:1}}
      #${MODAL_ID} .va-ai-card{
        width:min(880px,calc(100vw - 40px));
        max-height:calc(100vh - 60px);
        display:flex;flex-direction:column;
        border-radius:22px;
        border:1px solid rgba(196,181,253,.32);
        background:
          linear-gradient(135deg,rgba(45,25,85,.94),rgba(28,14,60,.92) 60%,rgba(43,20,92,.94));
        box-shadow:
          0 30px 80px -20px rgba(8,4,32,.6),
          0 0 0 1px rgba(255,255,255,.05) inset,
          0 0 60px rgba(139,92,246,.22);
        color:#eef1fb;font-family:inherit;overflow:hidden;
      }
      #${MODAL_ID} .va-ai-hdr{
        display:flex;align-items:center;justify-content:space-between;
        padding:18px 22px;border-bottom:1px solid rgba(196,181,253,.18);
        background:linear-gradient(90deg,rgba(139,92,246,.14),transparent);
      }
      #${MODAL_ID} .va-ai-hdr h2{
        margin:0;font-size:1.05rem;font-weight:600;letter-spacing:.02em;
        color:#f5f3ff;display:flex;gap:10px;align-items:center;
      }
      #${MODAL_ID} .va-ai-hdr .va-ai-badge{
        display:inline-block;padding:2px 10px;border-radius:999px;
        font-size:.7rem;letter-spacing:.08em;text-transform:uppercase;
        background:rgba(139,92,246,.22);border:1px solid rgba(196,181,253,.32);
        color:#c4b5fd;
      }
      #${MODAL_ID} .va-ai-close{
        background:transparent;border:1px solid rgba(226,232,240,.18);
        color:#c4b5fd;width:32px;height:32px;border-radius:9px;
        cursor:pointer;font-size:1.1rem;display:flex;align-items:center;justify-content:center;
        transition:background 160ms,border-color 160ms,transform 160ms;
      }
      #${MODAL_ID} .va-ai-close:hover{
        background:rgba(196,181,253,.14);border-color:rgba(196,181,253,.42);
        transform:translateY(-1px);
      }
      #${MODAL_ID} .va-ai-body{padding:18px 22px 6px;overflow:auto;flex:1;}
      #${MODAL_ID} .va-ai-grid{
        display:grid;grid-template-columns:1fr 1fr;gap:12px 16px;margin-bottom:12px;
      }
      #${MODAL_ID} .va-ai-full{grid-column:1 / -1;}
      #${MODAL_ID} .va-ai-field label{
        display:block;font-size:.72rem;letter-spacing:.06em;text-transform:uppercase;
        color:#c4b5fd;margin-bottom:5px;font-weight:600;
      }
      #${MODAL_ID} .va-ai-field label .req{color:#f472b6;margin-left:3px;}
      #${MODAL_ID} .va-ai-field input,
      #${MODAL_ID} .va-ai-field select,
      #${MODAL_ID} .va-ai-field textarea{
        width:100%;box-sizing:border-box;
        background:rgba(15,10,32,.55);
        border:1px solid rgba(196,181,253,.24);
        border-radius:10px;padding:9px 12px;font-size:.9rem;
        color:#eef1fb;font-family:inherit;
        transition:border-color 160ms,box-shadow 160ms,background 160ms;
      }
      #${MODAL_ID} .va-ai-field input:focus,
      #${MODAL_ID} .va-ai-field select:focus,
      #${MODAL_ID} .va-ai-field textarea:focus{
        outline:none;
        border-color:rgba(196,181,253,.62);
        box-shadow:0 0 0 3px rgba(139,92,246,.22);
        background:rgba(15,10,32,.72);
      }
      #${MODAL_ID} .va-ai-field textarea{
        min-height:170px;resize:vertical;
        font-family:"JetBrains Mono","Fira Code","SF Mono",monospace;
        font-size:.82rem;line-height:1.45;tab-size:4;
        white-space:pre;
      }
      #${MODAL_ID} .va-ai-field select{appearance:none;cursor:pointer;
        background-image:
          linear-gradient(45deg,transparent 50%,#c4b5fd 50%),
          linear-gradient(-45deg,transparent 50%,#c4b5fd 50%);
        background-position:calc(100% - 18px) 50%,calc(100% - 12px) 50%;
        background-size:6px 6px;background-repeat:no-repeat;padding-right:34px;
      }
      #${MODAL_ID} .va-ai-hint{
        font-size:.72rem;color:rgba(226,232,240,.62);margin-top:4px;line-height:1.35;
      }
      #${MODAL_ID} .va-ai-error{
        margin:0 0 10px;padding:9px 12px;border-radius:9px;
        background:rgba(244,63,94,.15);border:1px solid rgba(244,63,94,.4);
        color:#fecdd3;font-size:.82rem;
      }
      #${MODAL_ID} .va-ai-ftr{
        padding:14px 22px;border-top:1px solid rgba(196,181,253,.18);
        display:flex;justify-content:space-between;align-items:center;gap:14px;
        background:linear-gradient(90deg,transparent,rgba(139,92,246,.08));
      }
      #${MODAL_ID} .va-ai-ftr .va-ai-note{font-size:.72rem;color:rgba(226,232,240,.55);}
      #${MODAL_ID} .va-ai-btnrow{display:flex;gap:10px;}
      #${MODAL_ID} button.va-ai-btn{
        border-radius:10px;padding:9px 18px;font-size:.86rem;font-weight:600;
        cursor:pointer;transition:transform 160ms,box-shadow 160ms,background 160ms;
        letter-spacing:.02em;
      }
      #${MODAL_ID} button.va-ai-btn.secondary{
        background:rgba(255,255,255,.06);
        border:1px solid rgba(226,232,240,.18);color:#e2e8f0;
      }
      #${MODAL_ID} button.va-ai-btn.secondary:hover{
        background:rgba(196,181,253,.14);border-color:rgba(196,181,253,.32);
      }
      #${MODAL_ID} button.va-ai-btn.primary{
        background:linear-gradient(135deg,#6C2FCE,#8B5CF6 54%,#A78BFA);
        border:1px solid rgba(255,255,255,.22);color:#fff;
        box-shadow:0 10px 24px rgba(108,47,206,.32),inset 0 1px 0 rgba(255,255,255,.22);
      }
      #${MODAL_ID} button.va-ai-btn.primary:hover{
        transform:translateY(-1px);
        box-shadow:0 14px 28px rgba(108,47,206,.42),inset 0 1px 0 rgba(255,255,255,.24);
      }
      #${MODAL_ID} button.va-ai-btn:disabled{opacity:.5;cursor:not-allowed;transform:none;}
    `;
    const el = document.createElement("style");
    el.id = "va-add-issue-styles";
    el.textContent = css;
    document.head.appendChild(el);
  }

  // ── Modal DOM ──────────────────────────────────────────────────
  function buildModal() {
    const el = document.createElement("div");
    el.id = MODAL_ID;
    el.setAttribute("hidden", "");
    el.setAttribute("role", "dialog");
    el.setAttribute("aria-modal", "true");
    el.setAttribute("aria-labelledby", "va-ai-title");
    el.innerHTML = `
      <div class="va-ai-card" role="document">
        <div class="va-ai-hdr">
          <h2 id="va-ai-title">
            <span>✏️ Add issue to KB</span>
            <span class="va-ai-badge">curated</span>
          </h2>
          <button class="va-ai-close" data-va-action="cancel" aria-label="Close">✕</button>
        </div>
        <div class="va-ai-body">
          <div class="va-ai-error" hidden></div>
          <div class="va-ai-grid">
            <div class="va-ai-field va-ai-full">
              <label>Title<span class="req">*</span></label>
              <input name="title" type="text"
                placeholder="One-line summary of the bug pattern" autocomplete="off"/>
            </div>
            <div class="va-ai-field">
              <label>Program</label>
              <input name="program" type="text"
                placeholder="e.g. HMAB052P — or 'any'" autocomplete="off"/>
            </div>
            <div class="va-ai-field">
              <label>Category</label>
              <select name="bug_category">
                ${CATEGORIES.map(
                  (c) => `<option value="${c}">${c}</option>`,
                ).join("")}
              </select>
            </div>
            <div class="va-ai-field va-ai-full">
              <label>Error signature</label>
              <input name="error_signature" type="text"
                placeholder="Error message text (leave blank for output-diff bugs)" autocomplete="off"/>
            </div>
            <div class="va-ai-field va-ai-full">
              <label>Tags</label>
              <input name="tags" type="text"
                placeholder="Comma-separated (e.g. file_operation, IsSuccess, StatusMedd)" autocomplete="off"/>
            </div>
            <div class="va-ai-field va-ai-full">
              <label>Symptoms</label>
              <textarea name="symptoms" spellcheck="false"
                placeholder="What does this bug look like at runtime? (e.g. abend, silent output diff, wrong field value)"></textarea>
            </div>
            <div class="va-ai-field va-ai-full">
              <label>Root cause</label>
              <textarea name="root_cause" spellcheck="false"
                placeholder="Why the fix works. Include anti-patterns and 'DO NOT change X' rules — the AI reads this verbatim before suggesting fixes."></textarea>
            </div>
            <div class="va-ai-field va-ai-full">
              <label>Buggy code<span class="req">*</span></label>
              <textarea name="buggy_code" spellcheck="false"
                placeholder="// paste the buggy C# snippet here"></textarea>
            </div>
            <div class="va-ai-field va-ai-full">
              <label>Fixed code<span class="req">*</span></label>
              <textarea name="fixed_code" spellcheck="false"
                placeholder="// paste the corrected C# snippet here"></textarea>
            </div>
          </div>
        </div>
        <div class="va-ai-ftr">
          <span class="va-ai-note">
            The saved entry will be embedded and retrieved for similar future bugs.
          </span>
          <div class="va-ai-btnrow">
            <button class="va-ai-btn secondary" data-va-action="cancel">Cancel</button>
            <button class="va-ai-btn primary" data-va-action="save">✅ Save to KB</button>
          </div>
        </div>
      </div>
    `;

    el.addEventListener("click", (ev) => {
      // Close on backdrop click (but not on card).
      if (ev.target === el) closeModal();
    });
    el.querySelectorAll("[data-va-action=cancel]").forEach((b) =>
      b.addEventListener("click", closeModal),
    );
    el.querySelector("[data-va-action=save]").addEventListener("click", onSave);

    document.body.appendChild(el);
    return el;
  }

  function ensureModal() {
    injectStyles();
    return document.getElementById(MODAL_ID) || buildModal();
  }

  function openModal() {
    const m = ensureModal();
    // Clear previous state
    m.querySelectorAll("input,textarea").forEach((i) => (i.value = ""));
    const sel = m.querySelector("select[name=bug_category]");
    if (sel) sel.value = "other";
    m.querySelector(".va-ai-error").hidden = true;
    m.hidden = false;
    setTimeout(() => {
      const first = m.querySelector("input[name=title]");
      first && first.focus();
    }, 40);
    document.addEventListener("keydown", onEsc);
  }
  function closeModal() {
    const m = document.getElementById(MODAL_ID);
    if (m) m.hidden = true;
    document.removeEventListener("keydown", onEsc);
  }
  function onEsc(ev) {
    if (ev.key === "Escape") closeModal();
  }

  function collectValues(m) {
    const v = {};
    m.querySelectorAll("input,select,textarea").forEach((el) => {
      v[el.name] = el.value.trim();
    });
    return v;
  }

  function showError(m, msg) {
    const box = m.querySelector(".va-ai-error");
    box.textContent = msg;
    box.hidden = false;
    box.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function onSave() {
    const m = document.getElementById(MODAL_ID);
    if (!m) return;
    const v = collectValues(m);

    const errs = [];
    if (!v.title) errs.push("Title is required");
    if (!v.buggy_code || v.buggy_code.startsWith("//"))
      errs.push("Buggy code is empty or still contains the placeholder");
    if (!v.fixed_code || v.fixed_code.startsWith("//"))
      errs.push("Fixed code is empty or still contains the placeholder");
    if (errs.length) {
      showError(m, errs.join(" · "));
      return;
    }

    const template = serializeToTemplate(v);
    closeModal();
    // Give Chainlit a tick to remove the backdrop before we fill+submit.
    setTimeout(() => fillChatInputAndSubmit(template), 60);
  }

  // ── Input interception ─────────────────────────────────────────
  //
  // We watch every submit attempt in the chat composer. If the raw
  // text is exactly ``/add-issue`` (with any whitespace), we open the
  // modal instead — the user's message is consumed and never reaches
  // the server. The server-side wizard entry point still exists and
  // remains fully usable for anyone who prefers pasting a template
  // manually.
  function isAddIssueTrigger(text) {
    return /^\s*\/add-issues?\s*$/i.test(text || "");
  }

  function interceptSubmit() {
    document.addEventListener(
      "keydown",
      (ev) => {
        if (ev.key !== "Enter" || ev.shiftKey || ev.isComposing) return;
        const input = chatInput();
        if (!input || document.activeElement !== input) return;
        if (isAddIssueTrigger(input.value)) {
          ev.preventDefault();
          ev.stopPropagation();
          // Clear the input so the /add-issue text doesn't linger.
          const proto = Object.getPrototypeOf(input);
          const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
          setter && setter.call(input, "");
          input.dispatchEvent(new Event("input", { bubbles: true }));
          openModal();
        }
      },
      true, // capture — before Chainlit's own handler
    );

    document.addEventListener(
      "click",
      (ev) => {
        const btn = ev.target.closest(
          "#chat-submit, #message-composer button[type='submit'], " +
            "#message-composer button:has(svg.lucide-arrow-up)",
        );
        if (!btn) return;
        const input = chatInput();
        if (!input) return;
        if (isAddIssueTrigger(input.value)) {
          ev.preventDefault();
          ev.stopPropagation();
          const proto = Object.getPrototypeOf(input);
          const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
          setter && setter.call(input, "");
          input.dispatchEvent(new Event("input", { bubbles: true }));
          openModal();
        }
      },
      true,
    );
  }

  // ── Boot ───────────────────────────────────────────────────────
  function boot() {
    interceptSubmit();
    // Pre-inject styles so the first open is instant (no layout thrash).
    injectStyles();
    // Marker in the console so we can confirm the script has loaded.
    // eslint-disable-next-line no-console
    console.info("[va-add-issue] modal ready — type /add-issue or /add-issues to open");
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
