/* ============================================================================
   Pipeline HUD — floating vertical mission-control panel
   ============================================================================
   Pure frontend feature. Zero backend changes required — instead it watches
   for hidden marker <div>s that the Python side emits via cl.Message. This
   is the same pattern used elsewhere in the UI layer (drag-drop, dashboard).

   How it works
   ------------
   1. Python emits a hidden marker like:
        <div class="va-pipeline-marker"
             data-event="step_start" data-num="2" data-op="load"></div>
      wrapped in a cl.Message (which is hidden via CSS).
   2. This script's MutationObserver detects the marker, parses its
      dataset, updates the internal state, and re-renders the panel.
   3. Panel is a single fixed-position <div> injected into <body>.
      Slides in on "start", slides out on "complete" / "dismiss".

   Events supported (data-event value):
     start        data-baseline data-steps  (steps = "Copy,Load,Run,Unload,Compare,Close")
     step_start   data-num data-op          (mark step as active/blinking)
     step_done    data-num data-op          (mark step as completed ✓)
     step_fail    data-num data-op          (mark step as failed ✗)
     step_pause   data-num data-op          (compare-differences: amber pulse)
     resume       data-baseline data-from   (re-open panel from a specific step)
     complete                               (all done → auto-dismiss after 3s)
     dismiss                                (close immediately)

   Zero-impact guarantees:
   * Panel is position:fixed on the right edge — chat area unaffected
   * Doesn't touch input, composer, workflow cards, or any other UI
   * If pipeline-hud.js fails to load, the app behaves exactly as before
     (markers are just invisible messages)
   ============================================================================ */

(function () {
    "use strict";

    if (window.__vaPipelineHudLoaded) return;
    window.__vaPipelineHudLoaded = true;

    // ── Tunables ──────────────────────────────────────────────────────
    // How long the panel stays on screen after ALL steps complete
    // successfully, before it auto-slides away (milliseconds).
    //   4000  = 4 seconds  (default — quick glance)
    //   8000  = 8 seconds  (comfortable read)
    //   15000 = 15 seconds (linger)
    //   0     = stay open forever (user must click ×)
    // You can also override at runtime from the browser console:
    //   window.VA_PIPELINE_DISMISS_MS = 10000;
    const DEFAULT_DISMISS_MS = 20000;
    function getDismissMs() {
        const override = window.VA_PIPELINE_DISMISS_MS;
        if (typeof override === "number" && override >= 0) return override;
        return DEFAULT_DISMISS_MS;
    }

    // ── Internal state ────────────────────────────────────────────────
    const STATE = {
        active:    false,
        baseline:  "",
        steps:     [],      // [{op, title, state}]  state ∈ done|active|pending|fail|pause
        panel:     null,    // DOM element
        dismissTimer: null,
        // Live-clock state (Tier-C "PIPELINE · 3/6 · 42% · 03:14" feature)
        startTime:   0,     // ms since epoch (0 = not started)
        completedAt: 0,     // ms since epoch (0 = still running) — freezes clock
        clockInterval: null,
    };

    // ── Panel construction ────────────────────────────────────────────
    function buildPanel() {
        if (STATE.panel) return STATE.panel;

        const panel = document.createElement("div");
        panel.className = "va-pipeline-panel";
        panel.setAttribute("role", "status");
        panel.setAttribute("aria-live", "polite");
        panel.innerHTML = `
            <span class="va-pipeline-orb va-pipeline-orb-a" aria-hidden="true"></span>
            <span class="va-pipeline-orb va-pipeline-orb-b" aria-hidden="true"></span>
            <div class="va-pipeline-header">
                <div class="va-pipeline-title-row">
                    <span class="va-pipeline-icon" aria-hidden="true">📊</span>
                    <span class="va-pipeline-name">PIPELINE</span>
                    <span class="va-pipeline-clock-wrap">
                        <span class="va-pipeline-clock-icon" aria-hidden="true">⏱</span>
                        <span class="va-pipeline-clock" data-role="clock">00:00</span>
                    </span>
                    <button class="va-pipeline-close" type="button"
                            aria-label="Close pipeline HUD" title="Hide">×</button>
                </div>
                <div class="va-pipeline-subject" data-role="subject"></div>
            </div>
            <div class="va-pipeline-body" data-role="steps"></div>
            <div class="va-pipeline-footer" data-role="footer"></div>
        `;

        panel.querySelector(".va-pipeline-close").addEventListener("click", () => {
            hidePanel();
        });

        document.body.appendChild(panel);
        STATE.panel = panel;
        return panel;
    }

    function renderSteps() {
        if (!STATE.panel) return;
        const body = STATE.panel.querySelector('[data-role="steps"]');
        const parts = [];
        STATE.steps.forEach((step, idx) => {
            const num = idx + 1;
            const isLast = idx === STATE.steps.length - 1;
            let mark = String(num);
            if (step.state === "done") mark = "✓";
            else if (step.state === "fail") mark = "✗";

            parts.push(`
                <div class="va-pipeline-step" data-state="${escapeAttr(step.state)}"
                     data-op="${escapeAttr(step.op)}">
                    <div class="va-pipeline-circle-wrap">
                        <div class="va-pipeline-circle">
                            <span class="va-pipeline-mark">${escapeHtml(mark)}</span>
                            <span class="va-pipeline-orbit" aria-hidden="true"></span>
                            <span class="va-pipeline-orbit va-pipeline-orbit-2" aria-hidden="true"></span>
                        </div>
                    </div>
                    <div class="va-pipeline-meta">
                        <div class="va-pipeline-num">STEP ${num}</div>
                        <div class="va-pipeline-label">${escapeHtml(step.title)}</div>
                        <div class="va-pipeline-state">${escapeHtml(stateLabel(step.state))}</div>
                    </div>
                </div>
            `);
            if (!isLast) {
                // Connector state = min(prev.state, next.state) semantics:
                // - solid cyan if BOTH sides done
                // - animated if the following step is active
                // - dim otherwise
                const nextState = STATE.steps[idx + 1].state;
                let connState = "pending";
                if (step.state === "done" && (nextState === "done")) connState = "done";
                else if (nextState === "active") connState = "flowing";
                else if (step.state === "fail" || nextState === "fail") connState = "fail";
                parts.push(`<div class="va-pipeline-connector" data-state="${escapeAttr(connState)}"></div>`);
            }
        });
        body.innerHTML = parts.join("");
    }

    function stateLabel(s) {
        return ({
            done:    "Done",
            active:  "Running",
            pending: "Pending",
            fail:    "Failed",
            pause:   "Awaiting",
        })[s] || "Pending";
    }

    function updateSubject() {
        if (!STATE.panel) return;
        const el = STATE.panel.querySelector('[data-role="subject"]');
        if (el) el.textContent = STATE.baseline || "";
    }

    function updateFooter(text) {
        if (!STATE.panel) return;
        const el = STATE.panel.querySelector('[data-role="footer"]');
        if (el) el.textContent = text || "";
    }

    /**
     * Live elapsed clock in the header: "03:14".
     * Ticks every second while the workflow runs; freezes on complete/fail.
     */
    function startClock() {
        stopClock(); // safety: never double-tick
        STATE.startTime   = Date.now();
        STATE.completedAt = 0;
        updateClock();
        STATE.clockInterval = setInterval(updateClock, 1000);
        if (STATE.panel) {
            const el = STATE.panel.querySelector('[data-role="clock"]');
            if (el) el.removeAttribute("data-frozen");
        }
    }

    function stopClock() {
        if (STATE.clockInterval) {
            clearInterval(STATE.clockInterval);
            STATE.clockInterval = null;
        }
    }

    function freezeClock(reason) {
        // Freeze the displayed time at the current moment.
        STATE.completedAt = Date.now();
        updateClock();
        stopClock();
        if (STATE.panel) {
            const el = STATE.panel.querySelector('[data-role="clock"]');
            if (el) el.setAttribute("data-frozen", reason || "complete");
        }
    }

    function updateClock() {
        if (!STATE.panel || !STATE.startTime) return;
        const endTime = STATE.completedAt || Date.now();
        const elapsed = Math.max(0, Math.floor((endTime - STATE.startTime) / 1000));
        const mm = String(Math.floor(elapsed / 60)).padStart(2, "0");
        const ss = String(elapsed % 60).padStart(2, "0");
        const el = STATE.panel.querySelector('[data-role="clock"]');
        if (el) el.textContent = `${mm}:${ss}`;
    }

    function showPanel() {
        buildPanel();
        // Force reflow so slide-in animation triggers
        requestAnimationFrame(() => {
            STATE.panel.classList.add("va-pipeline-panel-open");
        });
        clearDismissTimer();
    }

    function hidePanel() {
        clearDismissTimer();
        stopClock();
        if (!STATE.panel) return;
        STATE.panel.classList.remove("va-pipeline-panel-open");
        STATE.panel.classList.add("va-pipeline-panel-closing");
        setTimeout(() => {
            if (STATE.panel && STATE.panel.parentNode) {
                STATE.panel.parentNode.removeChild(STATE.panel);
            }
            STATE.panel = null;
            STATE.active = false;
        }, 350);
    }

    function scheduleDismiss(ms) {
        clearDismissTimer();
        STATE.dismissTimer = setTimeout(hidePanel, ms);
    }

    function clearDismissTimer() {
        if (STATE.dismissTimer) {
            clearTimeout(STATE.dismissTimer);
            STATE.dismissTimer = null;
        }
    }

    // ── Event dispatcher ─────────────────────────────────────────────
    function handleEvent(event, data) {
        switch (event) {
            case "start": {
                STATE.active = true;
                STATE.baseline = data.baseline || "";
                // steps arg = "Copy,Load,Run,Unload,Compare,Close"
                // ops   arg = "copy,load,run,unload,compare,close"
                const titles = (data.steps || "").split(",").map(s => s.trim()).filter(Boolean);
                const ops    = (data.ops   || "").split(",").map(s => s.trim()).filter(Boolean);
                STATE.steps = titles.map((title, i) => ({
                    op:    ops[i] || title.toLowerCase(),
                    title,
                    state: "pending",
                }));
                showPanel();
                updateSubject();
                renderSteps();
                updateFooter("");
                startClock();
                break;
            }
            case "step_start": {
                const idx = Math.max(0, (parseInt(data.num, 10) || 1) - 1);
                // Any prior "active" step gets forced to done (safety)
                STATE.steps.forEach((s, i) => {
                    if (i < idx && s.state !== "fail" && s.state !== "pause") s.state = "done";
                });
                if (STATE.steps[idx]) STATE.steps[idx].state = "active";
                renderSteps();
                break;
            }
            case "step_done": {
                const idx = Math.max(0, (parseInt(data.num, 10) || 1) - 1);
                if (STATE.steps[idx]) STATE.steps[idx].state = "done";
                renderSteps();
                break;
            }
            case "step_fail": {
                const idx = Math.max(0, (parseInt(data.num, 10) || 1) - 1);
                if (STATE.steps[idx]) STATE.steps[idx].state = "fail";
                renderSteps();
                updateFooter("Failed — see details above");
                // Freeze clock — workflow ended here.
                freezeClock("fail");
                // Keep panel open — user needs to see failure
                break;
            }
            case "step_pause": {
                const idx = Math.max(0, (parseInt(data.num, 10) || 1) - 1);
                if (STATE.steps[idx]) STATE.steps[idx].state = "pause";
                renderSteps();
                updateFooter("Awaiting your decision above");
                break;
            }
            case "resume": {
                // Panel might have been dismissed — recreate it if needed.
                if (!STATE.panel || !STATE.active) {
                    // Re-open with existing state
                    STATE.active = true;
                    if (data.baseline) STATE.baseline = data.baseline;
                    showPanel();
                    updateSubject();
                    renderSteps();
                    // Resume the clock from wherever it was (don't reset elapsed
                    // — the workflow's really been running the whole time).
                    if (STATE.startTime && !STATE.clockInterval) {
                        STATE.completedAt = 0;
                        STATE.clockInterval = setInterval(updateClock, 1000);
                        const el = STATE.panel.querySelector('[data-role="clock"]');
                        if (el) el.removeAttribute("data-frozen");
                    }
                }
                updateFooter("");
                break;
            }
            case "complete": {
                // All steps done — mark any lingering active as done, then auto-dismiss
                STATE.steps.forEach(s => { if (s.state === "active") s.state = "done"; });
                renderSteps();
                updateFooter("✓ Complete");
                freezeClock("complete");
                const ms = getDismissMs();
                if (ms > 0) scheduleDismiss(ms);
                // ms === 0 → stay open until user clicks ×
                break;
            }
            case "dismiss": {
                hidePanel();
                break;
            }
            default:
                // Unknown event — ignore silently
                break;
        }
    }

    // ── Marker consumption ───────────────────────────────────────────
    // Python emits markers as markdown images with a special alt:
    //     ![va-pipeline?event=step_start&num=2&op=load](data:,)
    // Chainlit renders this as <img alt="va-pipeline?..." src="...">.
    // We parse the alt attribute as a URL query string.
    const MARKER_SELECTOR = 'img[alt^="va-pipeline?"]:not([data-va-processed])';
    const MARKER_PREFIX   = "va-pipeline?";

    function processMarker(el) {
        // Guard: only process each marker once.
        if (el.dataset.vaProcessed === "1") return;
        el.dataset.vaProcessed = "1";

        const alt = el.getAttribute("alt") || "";
        if (!alt.startsWith(MARKER_PREFIX)) return;
        const queryStr = alt.slice(MARKER_PREFIX.length);

        let params;
        try {
            params = new URLSearchParams(queryStr);
        } catch (e) {
            console.warn("[pipeline-hud] bad marker query:", queryStr, e);
            return;
        }

        const event = params.get("event") || "";
        const data = {};
        for (const [key, value] of params.entries()) {
            if (key !== "event") data[key] = value;
        }
        try {
            handleEvent(event, data);
        } catch (e) {
            console.error("[pipeline-hud] error handling event", event, e);
        }
    }

    function scanForMarkers(root) {
        const nodes = (root || document).querySelectorAll(MARKER_SELECTOR);
        nodes.forEach(processMarker);
    }

    // ── Init ─────────────────────────────────────────────────────────
    function init() {
        // First pass — pick up any markers that were already in the DOM.
        scanForMarkers(document);

        // Live watch for new markers.
        const obs = new MutationObserver((mutations) => {
            for (const mut of mutations) {
                for (const node of mut.addedNodes) {
                    if (node.nodeType !== 1) continue;
                    // Direct <img> insertion
                    if (node.tagName === "IMG" &&
                        (node.getAttribute("alt") || "").startsWith(MARKER_PREFIX)) {
                        processMarker(node);
                        continue;
                    }
                    // Or a container that holds one or more markers
                    if (node.querySelector) {
                        scanForMarkers(node);
                    }
                }
            }
        });
        obs.observe(document.body, { childList: true, subtree: true });
    }

    // ── Escape helpers ───────────────────────────────────────────────
    function escapeHtml(s) {
        return String(s == null ? "" : s)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    }
    function escapeAttr(s) { return escapeHtml(s); }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
