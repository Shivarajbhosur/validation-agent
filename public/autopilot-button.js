/* eslint-disable */
/**
 * Autopilot launcher button.
 *
 * Injects a floating "Autopilot" button onto the dashboard. Clicking it
 * sends "/autopilot" into the chat (same textarea-set + submit pattern
 * the add-issue modal uses), which enables Autopilot mode on the server.
 *
 * Fully additive and self-contained: all DOM/styles are scoped under
 * #va-autopilot-launch; if the chat input isn't found it no-ops. It never
 * touches the compare auto-fix flow or any other feature.
 */
(function () {
  if (window.__VA_AUTOPILOT_BOOT__) return;
  window.__VA_AUTOPILOT_BOOT__ = true;

  const BTN_ID = "va-autopilot-launch";

  function chatInput() {
    return (
      document.querySelector("#chat-input") ||
      document.querySelector("textarea[placeholder]") ||
      document.querySelector("textarea")
    );
  }

  function sendCommand(text) {
    const input = chatInput();
    if (!input) {
      console.warn("[va-autopilot] chat input not found");
      return;
    }
    const proto = Object.getPrototypeOf(input);
    const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
    setter && setter.call(input, text);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));

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

  function injectStyles() {
    if (document.getElementById("va-autopilot-style")) return;
    const s = document.createElement("style");
    s.id = "va-autopilot-style";
    s.textContent = `
      #${BTN_ID} {
        position: fixed; z-index: 2147483000;
        right: 22px; bottom: 96px;
        display: flex; align-items: center; gap: 8px;
        padding: 11px 18px; border: none; border-radius: 999px;
        font: 600 14px/1 ui-sans-serif, system-ui, sans-serif;
        color: #fff; cursor: pointer; letter-spacing: .2px;
        background: linear-gradient(135deg, #7c3aed 0%, #2563eb 55%, #06b6d4 100%);
        box-shadow: 0 8px 26px rgba(79,70,229,.45), 0 0 0 1px rgba(255,255,255,.08) inset;
        transition: transform .15s ease, box-shadow .15s ease, filter .15s ease;
      }
      #${BTN_ID}:hover { transform: translateY(-2px); filter: brightness(1.08);
        box-shadow: 0 12px 34px rgba(79,70,229,.6); }
      #${BTN_ID}:active { transform: translateY(0); }
      #${BTN_ID} .va-ap-ico { font-size: 16px; line-height: 1; }
      /* Active (engaged) state */
      #${BTN_ID}.va-ap-on {
        background: linear-gradient(135deg, #059669 0%, #10b981 55%, #34d399 100%);
        box-shadow: 0 8px 26px rgba(16,185,129,.5), 0 0 0 1px rgba(255,255,255,.12) inset;
      }
      #${BTN_ID}.va-ap-on:hover { box-shadow: 0 12px 34px rgba(16,185,129,.65); }
      #${BTN_ID} .va-ap-dot {
        width: 9px; height: 9px; border-radius: 50%; background: #d1fae5;
        box-shadow: 0 0 0 0 rgba(209,250,229,.8);
        animation: va-ap-pulse 1.4s ease-out infinite;
      }
      @keyframes va-ap-pulse {
        0%   { box-shadow: 0 0 0 0 rgba(209,250,229,.75); }
        70%  { box-shadow: 0 0 0 8px rgba(209,250,229,0); }
        100% { box-shadow: 0 0 0 0 rgba(209,250,229,0); }
      }
      @media (max-width: 640px) { #${BTN_ID} { right: 12px; bottom: 84px; padding: 10px 14px; } }
    `;
    document.head.appendChild(s);
  }

  let vaActive = false;

  function render(btn) {
    if (!btn) return;
    if (vaActive) {
      btn.classList.add("va-ap-on");
      btn.title = "Autopilot is ON — click to stop";
      btn.innerHTML =
        '<span class="va-ap-dot"></span><span>Autopilot Active</span>';
    } else {
      btn.classList.remove("va-ap-on");
      btn.title =
        "Autopilot — describe an issue in plain language and let the agent fix until files match";
      btn.innerHTML = '<span class="va-ap-ico">🛫</span><span>Autopilot</span>';
    }
  }

  function setActive(on) {
    if (vaActive === on) return;
    vaActive = on;
    render(document.getElementById(BTN_ID));
  }

  function mount() {
    if (document.getElementById(BTN_ID)) return;
    if (!chatInput()) return; // wait until Chainlit UI is ready
    injectStyles();
    const btn = document.createElement("button");
    btn.id = BTN_ID;
    btn.type = "button";
    btn.addEventListener("click", function () {
      if (vaActive) {
        // Toggle OFF — stop the session.
        sendCommand("/autopilot stop");
        setActive(false);
      } else {
        // Toggle ON — enable the session.
        sendCommand("/autopilot");
        setActive(true);
      }
    });
    document.body.appendChild(btn);
    render(btn);
  }

  // Keep the button in sync with server-side session start/stop by
  // watching assistant messages for Autopilot's own status markers.
  function syncFromMessages() {
    const text = document.body ? document.body.textContent || "" : "";
    if (
      text.includes("Autopilot disabled") ||
      text.includes("Autopilot Verified — All Files Match") ||
      text.includes("Autopilot stopped without full match") ||
      text.includes("nothing to fix")
    ) {
      setActive(false);
    } else if (
      text.includes("Autopilot enabled") ||
      text.includes("Autopilot engaged")
    ) {
      setActive(true);
    }
  }

  // Mount now + keep it present across Chainlit re-renders.
  function boot() {
    mount();
    const obs = new MutationObserver(function () {
      if (!document.getElementById(BTN_ID)) mount();
      syncFromMessages();
    });
    obs.observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
