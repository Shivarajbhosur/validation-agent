/* ============================================================================
   Drag & Drop workflow cards → chat composer
   ============================================================================
   Pure frontend feature — zero backend changes.
   
   Behaviour
   ---------
   1. Every workflow card (Step 1..8) and utility card (status/reset/list/help/
      validate/reporting) in the welcome message becomes draggable.
   2. When the user drags a card over the message composer, the composer
      highlights with a gold+cyan glow and shows a "⬇ Drop to run" pill.
   3. On drop:
        • Command WITHOUT <baseline>  → auto-submits immediately
                                        (list, help, and any other no-arg cmd)
        • Command WITH <baseline>     → opens a premium modal to collect
                                        the baseline, then auto-submits
   4. Recent baselines are stored in localStorage and offered as
      one-click pills inside the modal.
   
   Robust because:
   * MutationObserver re-scans for cards as Chainlit re-renders / streams
   * setInputValue uses the React-safe native-setter dance
   * Send button lookup tries multiple selectors (Chainlit versions differ)
   ============================================================================ */

(function () {
    "use strict";

    // Guard against double-load if voice.js loader runs twice.
    if (window.__vaDragDropLoaded) return;
    window.__vaDragDropLoaded = true;

    // ── Constants ─────────────────────────────────────────────────────
    const BASELINE_RE = /^P\d{4,6}(?:\.\d+)?-[A-Z]{1,6}\d{2,8}[A-Z]?$/;
    const RECENT_KEY = "va_recent_baselines";
    const MAX_RECENT = 8;
    const ATTACHED_FLAG = "vaDraggable";      // dataset key
    const CARD_SELECTOR = [
        '.ai-message h1 ~ * table tr',
        '.ai-message:has(h1) table tr',
    ].join(', ');

    // ── DOM helpers ───────────────────────────────────────────────────
    function getInput() {
        return (
            document.querySelector("#chat-input") ||
            document.querySelector("#message-composer textarea") ||
            document.querySelector("textarea[placeholder]") ||
            document.querySelector("textarea")
        );
    }

    function getComposer() {
        return document.querySelector("#message-composer");
    }

    function getSendButton() {
        // Chainlit's send button selector varies by version — try many.
        return (
            document.querySelector('#chat-submit') ||
            document.querySelector('#message-composer button[type="submit"]') ||
            document.querySelector('button[data-testid="send-message"]') ||
            document.querySelector('button[data-testid="send"]') ||
            document.querySelector('#message-composer button[aria-label*="Send" i]') ||
            document.querySelector('button[aria-label*="Send" i]') ||
            document.querySelector('#message-composer button[type="button"]:last-of-type') ||
            document.querySelector('#message-composer button:last-of-type')
        );
    }

    /**
     * Fire a full mouse-event sequence on the button. Some React handlers
     * react on `mousedown` instead of `click`; dispatching the full sequence
     * covers both cases.
     */
    function fireClick(btn) {
        const opts = { bubbles: true, cancelable: true, view: window, button: 0 };
        try { btn.dispatchEvent(new MouseEvent("pointerdown", opts)); } catch (_) {}
        btn.dispatchEvent(new MouseEvent("mousedown", opts));
        try { btn.dispatchEvent(new MouseEvent("pointerup", opts)); } catch (_) {}
        btn.dispatchEvent(new MouseEvent("mouseup", opts));
        btn.dispatchEvent(new MouseEvent("click", opts));
    }

    /**
     * Dispatch a full Enter keypress sequence. Some Chainlit builds listen
     * for Enter on the textarea instead of using a Send button.
     */
    function fireEnter(input) {
        const opts = {
            key: "Enter", code: "Enter", keyCode: 13, which: 13,
            bubbles: true, cancelable: true,
        };
        input.dispatchEvent(new KeyboardEvent("keydown", opts));
        input.dispatchEvent(new KeyboardEvent("keypress", opts));
        input.dispatchEvent(new KeyboardEvent("keyup", opts));
    }

    /**
     * Set the composer input to `cmd` and auto-submit.
     *
     * Because Chainlit's <textarea> is a React-controlled component, we
     * (a) use the native prototype setter to update the DOM value,
     * (b) dispatch bubbling input+change so React's onChange fires,
     * (c) then POLL for a submittable send button across a few animation
     *     frames — this lets React's state reconcile before we click.
     */
    function submitCommand(cmd) {
        console.log("[drag-drop] submitting:", cmd);
        const input = getInput();
        if (!input) {
            console.warn("[drag-drop] no chat input found; cannot submit:", cmd);
            return;
        }
        setInputValue(input, cmd);

        let attempts = 0;
        const MAX_ATTEMPTS = 15;   // ~250ms at 60fps

        function trySubmit() {
            attempts++;
            const btn = getSendButton();
            if (btn && !btn.disabled && !btn.getAttribute("aria-disabled")) {
                console.log(`[drag-drop] send button ready (attempt ${attempts}):`, btn);
                fireClick(btn);
                return;
            }
            if (attempts < MAX_ATTEMPTS) {
                requestAnimationFrame(trySubmit);
                return;
            }
            // Give up on button — try form submit, then Enter key.
            console.log(`[drag-drop] send button not ready after ${attempts} attempts (btn=${btn}, disabled=${btn && btn.disabled}); falling back`);
            const form = input.closest("form");
            if (form) {
                console.log("[drag-drop] falling back to form submit");
                if (typeof form.requestSubmit === "function") {
                    form.requestSubmit();
                } else {
                    form.submit();
                }
                return;
            }
            console.log("[drag-drop] falling back to Enter key");
            fireEnter(input);
        }
        requestAnimationFrame(trySubmit);
    }

    /**
     * React-safe setter — Chainlit's input is controlled by React state.
     * Setting `.value` directly won't fire onChange; we must call the
     * prototype setter and dispatch bubbling input+change events.
     */
    function setInputValue(input, value) {
        const proto = Object.getPrototypeOf(input);
        const desc = Object.getOwnPropertyDescriptor(proto, "value");
        if (desc && typeof desc.set === "function") {
            desc.set.call(input, value);
        } else {
            input.value = value;
        }
        input.dispatchEvent(new Event("input", { bubbles: true }));
        input.dispatchEvent(new Event("change", { bubbles: true }));
        input.focus();
        try { input.setSelectionRange(value.length, value.length); }
        catch (_) { /* not all inputs support selection */ }
    }

    // ── Card → command extraction ─────────────────────────────────────
    /**
     * Given a <tr> row, extract the command text (e.g. "copy <baseline>").
     * The command lives inside the first <code> element in the row.
     * Returns null if no code element found (not a real command row).
     */
    function extractCommand(row) {
        const code = row.querySelector("code");
        if (!code) return null;
        const txt = (code.textContent || "").trim();
        if (!txt) return null;
        return txt;
    }

    /**
     * Get the human label of the row (last cell's textContent) — used for
     * modal title only. Falls back to the command itself if missing.
     */
    function extractLabel(row) {
        const cells = row.querySelectorAll("td, th");
        if (!cells.length) return "";
        const last = cells[cells.length - 1];
        return (last.textContent || "").trim();
    }

    // ── Recent baselines (localStorage) ──────────────────────────────
    function getRecentBaselines() {
        try {
            const raw = localStorage.getItem(RECENT_KEY);
            const arr = raw ? JSON.parse(raw) : [];
            return Array.isArray(arr) ? arr.filter(b => BASELINE_RE.test(b)) : [];
        } catch (_) { return []; }
    }

    function saveRecentBaseline(bl) {
        if (!BASELINE_RE.test(bl)) return;
        const recent = getRecentBaselines().filter(b => b !== bl);
        recent.unshift(bl);
        try {
            localStorage.setItem(RECENT_KEY, JSON.stringify(recent.slice(0, MAX_RECENT)));
        } catch (_) { /* quota exceeded, ignore */ }
    }

    // ── Baseline modal ────────────────────────────────────────────────
    let activeModal = null;

    function closeModal() {
        if (!activeModal) return;
        activeModal.classList.add("va-modal-closing");
        setTimeout(() => {
            if (activeModal && activeModal.parentNode) {
                activeModal.parentNode.removeChild(activeModal);
            }
            activeModal = null;
        }, 180);
    }

    /**
     * Show a premium modal asking for a baseline. On submit, replaces
     * "<baseline>" in cmdTemplate with the user input and submits.
     */
    function showBaselineModal(cmdTemplate, cardLabel) {
        // If a modal is already open (rare double-drop), close it first.
        if (activeModal) closeModal();

        const backdrop = document.createElement("div");
        backdrop.className = "va-modal-backdrop";
        backdrop.innerHTML = `
            <div class="va-modal" role="dialog" aria-modal="true" aria-labelledby="va-modal-title">
                <div class="va-modal-header">
                    <div class="va-modal-title" id="va-modal-title">
                        <span class="va-modal-emoji" aria-hidden="true">⚡</span>
                        <span class="va-modal-title-text">Run <code>${escapeHtml(cmdTemplate)}</code></span>
                    </div>
                    <button class="va-modal-close" type="button" aria-label="Cancel">×</button>
                </div>
                ${cardLabel ? `<div class="va-modal-sub">${escapeHtml(cardLabel)}</div>` : ""}
                <div class="va-modal-body">
                    <label class="va-modal-label" for="va-modal-input">Baseline name</label>
                    <input
                        id="va-modal-input"
                        class="va-modal-input"
                        type="text"
                        placeholder="P02070-HPPL486P"
                        autocomplete="off"
                        spellcheck="false"
                    />
                    <div class="va-modal-hint" data-role="hint">Format: <code>P00000-XXXX000X</code></div>
                    <div class="va-modal-recent-wrap" data-role="recent"></div>
                </div>
                <div class="va-modal-actions">
                    <button class="va-modal-btn va-modal-btn-secondary" type="button" data-action="cancel">Cancel</button>
                    <button class="va-modal-btn va-modal-btn-primary" type="button" data-action="submit" disabled>
                        Run <code>${escapeHtml(cmdTemplate.split(' ')[0])}</code>
                    </button>
                </div>
            </div>
        `;

        document.body.appendChild(backdrop);
        activeModal = backdrop;

        const input      = backdrop.querySelector("#va-modal-input");
        const hint       = backdrop.querySelector('[data-role="hint"]');
        const recentWrap = backdrop.querySelector('[data-role="recent"]');
        const submitBtn  = backdrop.querySelector('[data-action="submit"]');
        const cancelBtn  = backdrop.querySelector('[data-action="cancel"]');
        const closeBtn   = backdrop.querySelector(".va-modal-close");

        // Populate recent-baseline pills
        const recent = getRecentBaselines();
        if (recent.length) {
            const pills = recent.map(bl =>
                `<button type="button" class="va-modal-recent-pill" data-baseline="${escapeAttr(bl)}">${escapeHtml(bl)}</button>`
            ).join("");
            recentWrap.innerHTML = `
                <div class="va-modal-recent-label">Recent baselines:</div>
                <div class="va-modal-recent-pills">${pills}</div>
            `;
            recentWrap.querySelectorAll(".va-modal-recent-pill").forEach(pill => {
                pill.addEventListener("click", () => {
                    input.value = pill.dataset.baseline;
                    input.dispatchEvent(new Event("input", { bubbles: true }));
                    input.focus();
                });
            });
        }

        // Focus input after the fade-in animation starts
        setTimeout(() => input.focus(), 50);

        // Live validation
        function validate() {
            const val = (input.value || "").trim().toUpperCase();
            const ok = BASELINE_RE.test(val);
            submitBtn.disabled = !ok;
            input.classList.toggle("va-modal-input-invalid", val.length > 0 && !ok);
            hint.classList.toggle("va-modal-hint-error", val.length > 0 && !ok);
            return ok;
        }

        function submit() {
            const val = (input.value || "").trim().toUpperCase();
            if (!BASELINE_RE.test(val)) {
                input.classList.add("va-modal-input-shake");
                setTimeout(() => input.classList.remove("va-modal-input-shake"), 400);
                return;
            }
            saveRecentBaseline(val);
            const finalCmd = cmdTemplate.replace(/<baseline>/gi, val);
            closeModal();
            submitCommand(finalCmd);
        }

        input.addEventListener("input", () => {
            // Auto-uppercase for user convenience
            const cursor = input.selectionStart;
            input.value = input.value.toUpperCase();
            try { input.setSelectionRange(cursor, cursor); } catch (_) {}
            validate();
        });

        input.addEventListener("keydown", (e) => {
            if (e.key === "Enter") { e.preventDefault(); submit(); }
            else if (e.key === "Escape") { e.preventDefault(); closeModal(); }
        });

        submitBtn.addEventListener("click", submit);
        cancelBtn.addEventListener("click", closeModal);
        closeBtn.addEventListener("click", closeModal);

        // Click outside modal panel closes it
        backdrop.addEventListener("click", (e) => {
            if (e.target === backdrop) closeModal();
        });

        // Global Esc
        function escHandler(e) {
            if (e.key === "Escape") {
                closeModal();
                document.removeEventListener("keydown", escHandler);
            }
        }
        document.addEventListener("keydown", escHandler);
    }

    // ── Drag start / end on cards ─────────────────────────────────────
    function onDragStart(e) {
        const row = e.currentTarget;
        const cmd = row.dataset.vaCommand;
        if (!cmd) return;
        try {
            e.dataTransfer.setData("text/plain", cmd);
            e.dataTransfer.setData("application/x-va-command", cmd);
            e.dataTransfer.effectAllowed = "copy";
        } catch (_) { /* IE / edge cases */ }
        document.body.classList.add("va-dragging");
        row.classList.add("va-dragging-source");
    }

    function onDragEnd(e) {
        const row = e.currentTarget;
        document.body.classList.remove("va-dragging");
        row.classList.remove("va-dragging-source");
        // Ensure composer clears its drop-active class in case dragleave
        // missed (happens across some drag paths).
        const composer = getComposer();
        if (composer) composer.classList.remove("va-drop-active");
    }

    /**
     * Scan the DOM for cards, mark unhandled ones as draggable, and
     * attach dragstart / dragend listeners. Uses the ATTACHED_FLAG
     * dataset key to avoid re-attaching to the same row.
     */
    function attachDraggableToCards() {
        const rows = document.querySelectorAll(CARD_SELECTOR);
        rows.forEach(row => {
            if (row.dataset[ATTACHED_FLAG] === "1") return;
            const cmd = extractCommand(row);
            if (!cmd) return;  // header row or a row without a command code
            row.dataset[ATTACHED_FLAG] = "1";
            row.dataset.vaCommand = cmd;
            row.dataset.vaLabel = extractLabel(row);
            row.setAttribute("draggable", "true");
            row.setAttribute("title", `Drag to run: ${cmd}`);
            row.addEventListener("dragstart", onDragStart);
            row.addEventListener("dragend", onDragEnd);
        });
    }

    // ── Drop target on message composer ──────────────────────────────
    // Historical note: we tried attaching dragover/drop listeners directly
    // to #message-composer, but Chainlit's inner textarea + surrounding
    // React handlers intercept the events before they reach our listener
    // (drop event never fires). Solution: bind at DOCUMENT level and check
    // whether the current target is inside the composer. Document-level
    // listeners always fire because nothing above them can stop propagation.
    let dropListenersInstalled = false;

    function installDocumentDropListeners() {
        if (dropListenersInstalled) return;
        dropListenersInstalled = true;

        document.addEventListener("dragover", (e) => {
            // Only care when the drag originated from one of our cards.
            if (!document.body.classList.contains("va-dragging")) return;
            const composer = getComposer();
            if (!composer) return;
            const over = composer.contains(e.target) || e.target === composer;
            if (over) {
                e.preventDefault();       // ← REQUIRED to allow the drop
                if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
                composer.classList.add("va-drop-active");
            } else {
                composer.classList.remove("va-drop-active");
            }
        }, true);   // capture phase — beats any child stopPropagation

        document.addEventListener("drop", (e) => {
            if (!document.body.classList.contains("va-dragging")) return;
            const composer = getComposer();
            if (!composer) return;
            const over = composer.contains(e.target) || e.target === composer;
            if (!over) return;
            e.preventDefault();
            e.stopPropagation();
            composer.classList.remove("va-drop-active");
            const cmd = (
                e.dataTransfer.getData("application/x-va-command") ||
                e.dataTransfer.getData("text/plain") ||
                ""
            ).trim();
            console.log("[drag-drop] drop landed, cmd=", cmd);
            if (cmd) {
                handleDroppedCommand(cmd);
            }
        }, true);   // capture phase — beats Chainlit's textarea drop handler

        // Also clear the drop-active glow if the user drops outside the
        // composer (browsers don't fire drop then, only dragend).
        document.addEventListener("dragend", () => {
            const composer = getComposer();
            if (composer) composer.classList.remove("va-drop-active");
        }, true);
    }

    // ── Route the dropped command ────────────────────────────────────
    function handleDroppedCommand(cmdTemplate) {
        if (cmdTemplate.includes("<baseline>")) {
            // Need to collect baseline via modal.
            showBaselineModal(cmdTemplate, "");
        } else {
            // No arg needed — send directly.
            submitCommand(cmdTemplate);
        }
    }

    // ── HTML-escape helpers ──────────────────────────────────────────
    function escapeHtml(s) {
        return String(s == null ? "" : s)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }
    function escapeAttr(s) { return escapeHtml(s); }

    // ── Init + MutationObserver ──────────────────────────────────────
    function init() {
        attachDraggableToCards();
        installDocumentDropListeners();
    }

    function startObserver() {
        const obs = new MutationObserver(() => {
            attachDraggableToCards();
            // Document-level drop listeners are one-shot; they don't
            // need re-installing when Chainlit re-renders.
        });
        obs.observe(document.body, { childList: true, subtree: true });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", () => {
            init();
            startObserver();
        });
    } else {
        init();
        startObserver();
    }
})();
