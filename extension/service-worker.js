const BRIDGE_PROTOCOL = "llm-wiki-google-docs-extension/v2";
const NATIVE_HOST = "net.llmwiki.google_docs";
const LAST_NORMAL_WINDOW_KEY = "lastNormalWindowId";
const CONNECTOR_STATE_KEY = "nativeConnectorState";
let nativePort = null;
let activeJob = null;
let reconnectTimer = null;
let pendingBoundary = null;

async function setConnectorState(state, detail = "") {
  await chrome.storage.session.set({
    [CONNECTOR_STATE_KEY]: { state, detail: String(detail || "").slice(0, 240) },
  });
  const badge = state === "working" ? "…" : state === "connected" ? "" : "!";
  const color = state === "working" ? "#B7791F" : state === "connected" ? "#2F855A" : "#C53030";
  await chrome.action.setBadgeText({ text: badge });
  await chrome.action.setBadgeBackgroundColor({ color });
}

async function configureExtension() {
  // Start the governed transport first. Side-panel setup is optional UI and
  // must never prevent the native connector from coming online.
  connectNativeBridge();
  await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
  await rememberCurrentNormalWindow();
}

chrome.runtime.onInstalled.addListener(configureExtension);
chrome.runtime.onStartup.addListener(configureExtension);
configureExtension().catch(() => {});

function nativeSend(value) {
  if (!nativePort) throw new Error("The local native connector is offline.");
  nativePort.postMessage({ protocol: BRIDGE_PROTOCOL, ...value });
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectNativeBridge();
  }, 1500);
}

function connectNativeBridge() {
  if (nativePort) return;
  try {
    const port = chrome.runtime.connectNative(NATIVE_HOST);
    nativePort = port;
    setConnectorState("connecting").catch(() => {});
    port.onMessage.addListener((message) => {
      handleNativeMessage(message).catch((error) => {
        const detail = error instanceof Error ? error.message : "Extension operation failed.";
        if (activeJob) {
          nativeSend({
            type: "result",
            result: {
              job_id: activeJob.job_id,
              status: "error",
              mode_verified: false,
              edit_count: 0,
              mutation_started: false,
              error: detail.slice(0, 500),
            },
          });
          activeJob = null;
        }
        setConnectorState("error", detail).catch(() => {});
      });
    });
    port.onDisconnect.addListener(() => {
      nativePort = null;
      activeJob = null;
      if (pendingBoundary) {
        pendingBoundary.reject(new Error("The local native connector disconnected."));
        pendingBoundary = null;
      }
      setConnectorState("offline", chrome.runtime.lastError?.message || "Native host disconnected.").catch(() => {});
      scheduleReconnect();
    });
  } catch (error) {
    nativePort = null;
    setConnectorState("offline", error instanceof Error ? error.message : "Native host unavailable.").catch(() => {});
    scheduleReconnect();
  }
}

function validateJob(job) {
  if (job.protocol !== BRIDGE_PROTOCOL || typeof job.job_id !== "string") {
    throw new Error("The local adapter returned an invalid edit job.");
  }
  if (!/^[a-f0-9]{64}$/.test(job.plan_sha256 || "")) {
    throw new Error("The edit job has no valid approved-plan hash.");
  }
  if (typeof job.document_id !== "string" || !job.document_id) {
    throw new Error("The edit job has no document target.");
  }
  if (!Array.isArray(job.edits) || job.edits.length < 1 || job.edits.length > 100) {
    throw new Error("The edit job must contain 1–100 exact replacements.");
  }
  for (const edit of job.edits) {
    if (
      typeof edit.tab_id !== "string" ||
      typeof edit.find !== "string" ||
      !edit.find ||
      typeof edit.replace !== "string" ||
      edit.find === edit.replace
    ) {
      throw new Error("The edit job contains an invalid exact replacement.");
    }
  }
  return job;
}

async function requestMutationBoundary(jobId) {
  if (pendingBoundary) throw new Error("A mutation boundary is already pending.");
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      if (pendingBoundary) pendingBoundary = null;
      reject(new Error("The local adapter did not authorize the mutation boundary in time."));
    }, 30000);
    pendingBoundary = {
      jobId,
      resolve: (value) => { clearTimeout(timeout); pendingBoundary = null; resolve(value); },
      reject: (error) => { clearTimeout(timeout); pendingBoundary = null; reject(error); },
    };
    nativeSend({ type: "before-mutation", job_id: jobId });
  });
}

function documentIdFromUrl(rawUrl) {
  let url;
  try {
    url = new URL(rawUrl);
  } catch (_error) {
    return "";
  }
  if (url.protocol !== "https:" || url.hostname !== "docs.google.com") {
    return "";
  }
  const match = url.pathname.match(/^\/document\/d\/([^/]+)\//);
  return match ? decodeURIComponent(match[1]) : "";
}

function documentTabIdFromUrl(rawUrl) {
  try {
    const url = new URL(rawUrl);
    return url.searchParams.get("tab") || "";
  } catch (_error) {
    return "";
  }
}

async function rememberNormalWindow(windowId) {
  if (!Number.isInteger(windowId) || windowId === chrome.windows.WINDOW_ID_NONE) return;
  try {
    const window = await chrome.windows.get(windowId);
    if (window.type === "normal") {
      await chrome.storage.session.set({ [LAST_NORMAL_WINDOW_KEY]: windowId });
    }
  } catch (_error) {
    // A window can close between the focus event and this lookup.
  }
}

async function rememberCurrentNormalWindow() {
  try {
    const window = await chrome.windows.getLastFocused({ windowTypes: ["normal"] });
    await rememberNormalWindow(window && window.id);
  } catch (_error) {
    // Chrome may have no focused normal window while the worker starts.
  }
}

async function mostRecentlyFocusedNormalWindow() {
  try {
    const current = await chrome.windows.getLastFocused({ windowTypes: ["normal"] });
    if (
      current && Number.isInteger(current.id) &&
      current.id !== chrome.windows.WINDOW_ID_NONE
    ) {
      await rememberNormalWindow(current.id);
      return current;
    }
  } catch (_error) {
    // Fall through to the last non-NONE focus event stored for this session.
  }
  const stored = await chrome.storage.session.get(LAST_NORMAL_WINDOW_KEY);
  const windowId = stored[LAST_NORMAL_WINDOW_KEY];
  if (!Number.isInteger(windowId)) return null;
  try {
    const window = await chrome.windows.get(windowId);
    return window.type === "normal" ? window : null;
  } catch (_error) {
    await chrome.storage.session.remove(LAST_NORMAL_WINDOW_KEY);
    return null;
  }
}

async function focusedDocumentTab(documentId) {
  const [tabs, focusedWindow] = await Promise.all([
    chrome.tabs.query({ url: "https://docs.google.com/document/*" }),
    mostRecentlyFocusedNormalWindow(),
  ]);
  if (!focusedWindow || typeof focusedWindow.id !== "number") {
    return null;
  }
  const matches = tabs.filter(
    (tab) => typeof tab.id === "number" && documentIdFromUrl(tab.url || "") === documentId,
  );
  return matches.find(
    (tab) => tab.windowId === focusedWindow.id && tab.active,
  ) || null;
}

async function activateDocumentTab(documentId) {
  const tabs = await chrome.tabs.query({ url: "https://docs.google.com/document/*" });
  let tab = tabs.find(
    (candidate) => typeof candidate.id === "number" && documentIdFromUrl(candidate.url || "") === documentId,
  ) || null;
  if (!tab) {
    const targetWindow = await mostRecentlyFocusedNormalWindow();
    const url = `https://docs.google.com/document/d/${encodeURIComponent(documentId)}/edit`;
    tab = await chrome.tabs.create({
      active: true,
      url,
      ...(targetWindow && Number.isInteger(targetWindow.id) ? { windowId: targetWindow.id } : {}),
    });
  } else {
    tab = await chrome.tabs.update(tab.id, { active: true });
  }
  if (!tab || !Number.isInteger(tab.id) || !Number.isInteger(tab.windowId)) {
    throw new Error("Chrome could not open the approved Google Doc.");
  }
  await chrome.windows.update(tab.windowId, { focused: true });
  await rememberNormalWindow(tab.windowId);
  return tab;
}

async function assertFocusedDocumentTab(tabId, documentId) {
  const tab = await focusedDocumentTab(documentId);
  if (!tab || tab.id !== tabId) {
    throw new Error(
      "The approved Google Doc lost focus before the governed mutation boundary.",
    );
  }
}

function debuggee(tabId) {
  return { tabId };
}

async function command(tabId, method, params = {}) {
  return chrome.debugger.sendCommand(debuggee(tabId), method, params);
}

async function evaluate(tabId, expression) {
  const response = await command(tabId, "Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (response.exceptionDetails) {
    throw new Error("Google Docs rejected a browser operation.");
  }
  return response.result ? response.result.value : undefined;
}

async function sleep(milliseconds) {
  await new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function waitFor(check, message, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      if (await check()) {
        return;
      }
    } catch (_error) {
      // Navigation can briefly destroy the current execution context.
    }
    await sleep(125);
  }
  throw new Error(message);
}

function visibleElementExpression(selector) {
  const encoded = JSON.stringify(selector);
  return `(() => {
    const element = Array.from(document.querySelectorAll(${encoded})).find((candidate) => {
      const rect = candidate.getBoundingClientRect();
      const style = getComputedStyle(candidate);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    });
    if (!element) return null;
    const rect = element.getBoundingClientRect();
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
  })()`;
}

async function clickSelector(tabId, selector) {
  const point = await evaluate(tabId, visibleElementExpression(selector));
  if (!point || !Number.isFinite(point.x) || !Number.isFinite(point.y)) {
    throw new Error("A required Google Docs control is unavailable.");
  }
  await command(tabId, "Input.dispatchMouseEvent", {
    type: "mousePressed", x: point.x, y: point.y, button: "left", clickCount: 1,
  });
  await command(tabId, "Input.dispatchMouseEvent", {
    type: "mouseReleased", x: point.x, y: point.y, button: "left", clickCount: 1,
  });
}

async function clickPoint(tabId, point) {
  if (!point || !Number.isFinite(point.x) || !Number.isFinite(point.y)) {
    throw new Error("A required Google Docs control is unavailable.");
  }
  await command(tabId, "Input.dispatchMouseEvent", {
    type: "mousePressed", x: point.x, y: point.y, button: "left", clickCount: 1,
  });
  await command(tabId, "Input.dispatchMouseEvent", {
    type: "mouseReleased", x: point.x, y: point.y, button: "left", clickCount: 1,
  });
}

function axValue(node, property) {
  const entry = node && node[property];
  return entry && entry.value !== undefined ? entry.value : "";
}

function axName(node) {
  return String(axValue(node, "name") || "").replace(/\s+/g, " ").trim().toLowerCase();
}

function axRole(node) {
  return String(axValue(node, "role") || "").replace(/\s+/g, "").toLowerCase();
}

async function accessibilityNodes(tabId) {
  const response = await command(tabId, "Accessibility.getFullAXTree");
  return Array.isArray(response.nodes) ? response.nodes : [];
}

function findReplaceAXControls(nodes) {
  const editable = nodes.filter((node) => {
    if (!node || node.ignored || !node.backendDOMNodeId) return false;
    return ["textbox", "textfield", "searchbox", "combobox"].includes(axRole(node));
  });
  const findInput = editable.find((node) => /^find\b/.test(axName(node))) || null;
  const replaceInput = editable.find((node) => /^replace\b/.test(axName(node))) || null;
  if (findInput && replaceInput && findInput !== replaceInput) {
    return { findInput, replaceInput };
  }
  const byId = new Map(nodes.filter((node) => node && node.nodeId).map((node) => [node.nodeId, node]));
  const descendants = (root) => {
    const found = [];
    const pending = Array.isArray(root.childIds) ? [...root.childIds] : [];
    const seen = new Set();
    while (pending.length) {
      const nodeId = pending.shift();
      if (seen.has(nodeId)) continue;
      seen.add(nodeId);
      const node = byId.get(nodeId);
      if (!node) continue;
      found.push(node);
      if (Array.isArray(node.childIds)) pending.push(...node.childIds);
    }
    return found;
  };
  for (const dialog of nodes.filter((node) => node && !node.ignored && axRole(node) === "dialog")) {
    const dialogEditable = descendants(dialog).filter((node) => (
      node && !node.ignored && node.backendDOMNodeId &&
      ["textbox", "textfield", "searchbox", "combobox"].includes(axRole(node))
    ));
    if (dialogEditable.length === 2) {
      return { findInput: dialogEditable[0], replaceInput: dialogEditable[1] };
    }
  }
  return { findInput, replaceInput };
}

async function findReplaceAXState(tabId) {
  return findReplaceAXControls(await accessibilityNodes(tabId));
}

async function axNodePoint(tabId, node) {
  if (!node || !node.backendDOMNodeId) return null;
  try {
    const response = await command(tabId, "DOM.getBoxModel", {
      backendNodeId: node.backendDOMNodeId,
    });
    const quad = response.model && (response.model.border || response.model.content);
    if (!Array.isArray(quad) || quad.length < 8) return null;
    const xs = [quad[0], quad[2], quad[4], quad[6]];
    const ys = [quad[1], quad[3], quad[5], quad[7]];
    return {
      x: xs.reduce((total, value) => total + value, 0) / xs.length,
      y: ys.reduce((total, value) => total + value, 0) / ys.length,
    };
  } catch (_error) {
    return null;
  }
}

async function clickAXNode(tabId, node) {
  const point = await axNodePoint(tabId, node);
  if (!point) return false;
  await clickPoint(tabId, point);
  return true;
}

async function focusAXNode(tabId, node) {
  if (!node || !node.backendDOMNodeId) return false;
  let focused = false;
  try {
    await command(tabId, "DOM.focus", { backendNodeId: node.backendDOMNodeId });
    focused = true;
  } catch (_error) {
    // Some Docs controls require a trusted mouse event instead of DOM.focus.
  }
  const point = await axNodePoint(tabId, node);
  if (point) {
    await clickPoint(tabId, point);
    focused = true;
  }
  return focused;
}

function namedAXNode(nodes, role, exactName) {
  const expected = exactName.toLowerCase();
  return nodes.find((node) => (
    node && !node.ignored && node.backendDOMNodeId &&
    axRole(node) === role.toLowerCase() && axName(node) === expected
  )) || null;
}

function axChecked(node) {
  const checked = Array.isArray(node && node.properties)
    ? node.properties.find((property) => property.name === "checked")
    : null;
  if (!checked || !checked.value) return false;
  return checked.value.value === true || checked.value.value === "true";
}

function axFocused(node) {
  const focused = Array.isArray(node && node.properties)
    ? node.properties.find((property) => property.name === "focused")
    : null;
  if (!focused || !focused.value) return false;
  return focused.value.value === true || focused.value.value === "true";
}

async function findReplaceAXHasUniqueMatch(tabId) {
  const nodes = await accessibilityNodes(tabId);
  return nodes.some((node) => /\b1\s+of\s+1\b/i.test(
    `${axValue(node, "name")} ${axValue(node, "value")}`,
  ));
}

async function findReplaceAXDiagnostics(tabId) {
  const nodes = await accessibilityNodes(tabId);
  const controls = findReplaceAXControls(nodes);
  const roleCount = (role) => nodes.filter((node) => !node.ignored && axRole(node) === role).length;
  return {
    dialog: roleCount("dialog"),
    textbox: roleCount("textbox") + roleCount("textfield") + roleCount("searchbox"),
    find: Boolean(controls.findInput),
    replace: Boolean(controls.replaceInput),
  };
}

async function clickNamedControl(tabId, role, exactName, rootSelector = "body") {
  const expression = `(() => {
    const root = document.querySelector(${JSON.stringify(rootSelector)});
    if (!root) return null;
    const expected = ${JSON.stringify(exactName.toLowerCase())};
    const candidates = Array.from(root.querySelectorAll(${JSON.stringify(`[role="${role}"],${role === "button" ? "button" : "input[type=checkbox]"}`)}));
    const element = candidates.find((candidate) => {
      const name = [candidate.getAttribute("aria-label"), candidate.getAttribute("data-tooltip"), candidate.textContent]
        .filter(Boolean).join(" ").trim().toLowerCase();
      const parentName = candidate.parentElement ? candidate.parentElement.innerText.trim().toLowerCase() : "";
      const rect = candidate.getBoundingClientRect();
      return (name === expected || parentName === expected) && rect.width > 0 && rect.height > 0;
    });
    if (!element) return null;
    const rect = element.getBoundingClientRect();
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
  })()`;
  const point = await evaluate(tabId, expression);
  if (!point) {
    throw new Error(`Google Docs ${exactName} control is unavailable.`);
  }
  await command(tabId, "Input.dispatchMouseEvent", {
    type: "mousePressed", x: point.x, y: point.y, button: "left", clickCount: 1,
  });
  await command(tabId, "Input.dispatchMouseEvent", {
    type: "mouseReleased", x: point.x, y: point.y, button: "left", clickCount: 1,
  });
}

function modeControlExpression() {
  return `(() => {
    const visible = (element) => {
      if (!element) return false;
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const label = (element) => [
      element.getAttribute("aria-label"),
      element.getAttribute("data-tooltip"),
      element.getAttribute("title"),
      element.textContent,
    ].filter(Boolean).join(" ").trim().toLowerCase();
    const exact = document.querySelector("#docs-mode-switcher-select");
    let element = visible(exact) ? exact : null;
    if (!element) {
      const candidates = Array.from(document.querySelectorAll(
        '[role="button"],[role="combobox"],[aria-haspopup="menu"],[aria-label],[data-tooltip],[title]'
      )).filter((candidate) => {
        if (!visible(candidate)) return false;
        const role = candidate.getAttribute("role") || "";
        const text = label(candidate);
        return !["menuitem", "menuitemradio", "option"].includes(role) &&
          text.length < 120 && /\\b(editing|suggesting|viewing)\\b/.test(text);
      });
      candidates.sort((left, right) => {
        const score = (candidate) => {
          const text = label(candidate);
          return (/mode/.test(text) ? 20 : 0) +
            (candidate.hasAttribute("aria-haspopup") ? 10 : 0) +
            (candidate.getBoundingClientRect().top < 350 ? 5 : 0);
        };
        return score(right) - score(left);
      });
      element = candidates[0] || null;
    }
    if (!element) return null;
    const rect = element.getBoundingClientRect();
    return { text: label(element), x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
  })()`;
}

function suggestingOptionExpression() {
  return `(() => {
    const visible = (element) => {
      if (!element) return false;
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const label = (element) => [
      element.getAttribute("aria-label"), element.getAttribute("data-tooltip"), element.textContent,
    ].filter(Boolean).join(" ").trim().toLowerCase();
    const exact = document.querySelector("#docs-mode-switcher-suggesting");
    const element = visible(exact) ? exact : Array.from(document.querySelectorAll(
      '[role="menuitem"],[role="menuitemradio"],[role="option"],[id*="mode-switcher"]'
    )).find((candidate) => visible(candidate) && /^suggesting\\b/.test(label(candidate)));
    if (!element) return null;
    const rect = element.getBoundingClientRect();
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
  })()`;
}

function suggestingDiagnosticsExpression() {
  return `(() => {
    const visible = (element) => {
      if (!element) return false;
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const label = (element) => [
      element.getAttribute("aria-label"), element.getAttribute("data-tooltip"), element.textContent,
    ].filter(Boolean).join(" ").trim().toLowerCase();
    const count = (selector) => Array.from(document.querySelectorAll(selector)).filter(visible).length;
    const named = Array.from(document.querySelectorAll(
      '[role="menuitem"],[role="menuitemradio"],[role="option"],[id*="mode-switcher"]'
    )).filter((candidate) => visible(candidate) && /^suggesting\\b/.test(label(candidate))).length;
    return {
      exact: visible(document.querySelector("#docs-mode-switcher-suggesting")),
      menuitem: count('[role="menuitem"]'),
      menuitemradio: count('[role="menuitemradio"]'),
      option: count('[role="option"]'),
      named,
    };
  })()`;
}

async function modeControl(tabId) {
  return evaluate(tabId, modeControlExpression());
}

async function waitForEditor(tabId) {
  await waitFor(
    async () => Boolean(await modeControl(tabId)),
    "The Google Docs editing-mode control did not become ready.",
    30000,
  );
}

async function ensureSuggesting(tabId) {
  let mode = await modeControl(tabId);
  if (!mode) {
    throw new Error("The Google Docs editing-mode control is unavailable.");
  }
  if (!mode.text.includes("suggest")) {
    const platform = String(await evaluate(tabId, "navigator.platform || ''"));
    const modifiers = platform.toLowerCase().includes("mac") ? 13 : 11;
    await dispatchShortcut(tabId, "x", "KeyX", modifiers);
    try {
      await waitFor(
        async () => {
          mode = await modeControl(tabId);
          return Boolean(mode && mode.text.includes("suggest"));
        },
        "",
        3000,
      );
      return;
    } catch (_error) {
      // Fall back to the visible mode menu when Docs does not handle the shortcut.
    }
    mode = await modeControl(tabId);
    if (!mode) {
      throw new Error("The Google Docs editing-mode control is unavailable.");
    }
    await clickPoint(tabId, mode);
    let option = null;
    try {
      await waitFor(
        async () => {
          option = await evaluate(tabId, suggestingOptionExpression());
          return Boolean(option);
        },
        "",
      );
    } catch (_error) {
      const diagnostics = await evaluate(tabId, suggestingDiagnosticsExpression());
      const suffix = diagnostics
        ? ` (exact=${diagnostics.exact}; menuitem=${diagnostics.menuitem}; menuitemradio=${diagnostics.menuitemradio}; option=${diagnostics.option}; named=${diagnostics.named})`
        : "";
      throw new Error(`Suggesting mode is not available for this account or document.${suffix}`);
    }
    await clickPoint(tabId, option);
  }
  await waitFor(
    async () => {
      mode = await modeControl(tabId);
      return Boolean(mode && mode.text.includes("suggest"));
    },
    "Google Docs did not confirm Suggesting mode.",
  );
}

function findReplaceContextExpression(body) {
  return `(() => {
    const visible = (element) => {
      if (!element) return false;
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const normalize = (value) => String(value || "").replace(/\\s+/g, " ").trim().toLowerCase();
    const label = (element) => normalize([
      element.getAttribute("aria-label"),
      element.getAttribute("data-tooltip"),
      element.getAttribute("placeholder"),
      element.getAttribute("title"),
    ].filter(Boolean).join(" "));
    const dialogs = Array.from(document.querySelectorAll(
      '[role="dialog"],[aria-modal="true"],.docs-dialog,.modal-dialog,.docs-findandreplacedialog'
    ))
      .filter(visible);
    const inputs = Array.from(document.querySelectorAll('input,[role="textbox"]')).filter(visible);
    let namedDialog = dialogs.find((candidate) => {
      const name = normalize([candidate.getAttribute("aria-label"), candidate.getAttribute("data-dialog-title")]
        .filter(Boolean).join(" "));
      const heading = Array.from(candidate.querySelectorAll('[role="heading"],h1,h2,h3,.docs-dialog-title'))
        .map((candidateHeading) => normalize(candidateHeading.textContent)).join(" ");
      const className = normalize(candidate.className);
      return /find( and| &) replace/.test(name) || /find( and| &) replace/.test(heading) ||
        /find.*replace/.test(className);
    }) || null;
    if (!namedDialog) {
      namedDialog = dialogs.find((candidate) => {
        const dialogInputs = inputs.filter((element) => candidate.contains(element));
        const buttons = Array.from(candidate.querySelectorAll('[role="button"],button')).filter(visible);
        const hasReplaceAction = buttons.some((button) => {
          const name = normalize([
            button.getAttribute("aria-label"), button.getAttribute("data-tooltip"), button.textContent,
          ].filter(Boolean).join(" "));
          return /^replace\b/.test(name);
        });
        return dialogInputs.length === 2 && hasReplaceAction;
      }) || null;
    }
    const score = (element, kind) => {
      const name = label(element);
      const className = normalize(element.className);
      if (kind === "find") {
        return (/find-input/.test(className) ? 100 : 0) + (/^find\\b/.test(name) ? 50 : 0);
      }
      return (/replace-input/.test(className) ? 100 : 0) + (/^replace\\b/.test(name) ? 50 : 0);
    };
    const ranked = (kind) => inputs.map((element) => ({ element, score: score(element, kind) }))
      .filter((candidate) => candidate.score > 0)
      .sort((left, right) => right.score - left.score);
    let findInput = ranked("find")[0]?.element || null;
    let replaceInput = ranked("replace")[0]?.element || null;
    if (namedDialog && (!findInput || !replaceInput)) {
      const dialogInputs = inputs.filter((element) => namedDialog.contains(element));
      findInput = findInput || dialogInputs[0] || null;
      replaceInput = replaceInput || dialogInputs.find((element) => element !== findInput) || null;
    }
    if (!findInput || !replaceInput || findInput === replaceInput) return null;
    let root = namedDialog && namedDialog.contains(findInput) && namedDialog.contains(replaceInput)
      ? namedDialog : findInput;
    while (root && !root.contains(replaceInput)) root = root.parentElement;
    if (!root) return null;
    ${body}
  })()`;
}

function findReplaceMenuItemExpression() {
  return `(() => {
    const normalize = (value) => String(value || "").replace(/\\s+/g, " ").trim().toLowerCase();
    const candidates = Array.from(document.querySelectorAll('[role="menuitem"],[role="option"]'));
    const element = candidates.find((candidate) => {
      const rect = candidate.getBoundingClientRect();
      const style = getComputedStyle(candidate);
      if (rect.width <= 0 || rect.height <= 0 || style.visibility === "hidden" || style.display === "none") return false;
      const name = normalize([
        candidate.getAttribute("aria-label"), candidate.getAttribute("data-tooltip"), candidate.textContent,
      ].filter(Boolean).join(" "));
      return name === "find and replace" || name.startsWith("find and replace ");
    });
    if (!element) return null;
    const rect = element.getBoundingClientRect();
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
  })()`;
}

function focusFindReplaceMenuItemExpression() {
  return `(() => {
    const normalize = (value) => String(value || "").replace(/\\s+/g, " ").trim().toLowerCase();
    const element = Array.from(document.querySelectorAll('[role="menuitem"],[role="option"]'))
      .find((candidate) => {
        const rect = candidate.getBoundingClientRect();
        const style = getComputedStyle(candidate);
        if (rect.width <= 0 || rect.height <= 0 || style.visibility === "hidden" || style.display === "none") return false;
        const name = normalize([
          candidate.getAttribute("aria-label"), candidate.getAttribute("data-tooltip"), candidate.textContent,
        ].filter(Boolean).join(" "));
        return name === "find and replace" || name.startsWith("find and replace ");
      });
    if (!element) return false;
    element.focus();
    return true;
  })()`;
}

async function findReplaceOpen(tabId) {
  const controls = await findReplaceAXState(tabId);
  if (controls.findInput && controls.replaceInput) return true;
  return Boolean(await evaluate(tabId, findReplaceContextExpression("return true;")));
}

async function findReplaceAXMenuItem(tabId) {
  const nodes = await accessibilityNodes(tabId);
  return nodes.find((node) => (
    node && !node.ignored && node.backendDOMNodeId &&
    ["menuitem", "menuitemradio"].includes(axRole(node)) &&
    axName(node).startsWith("find and replace")
  )) || null;
}

async function dispatchShortcut(tabId, key, code, modifiers) {
  await command(tabId, "Input.dispatchKeyEvent", {
    type: "rawKeyDown", key, code, modifiers,
  });
  await command(tabId, "Input.dispatchKeyEvent", {
    type: "keyUp", key, code, modifiers,
  });
}

async function waitForFindReplaceOpen(tabId, timeoutMs) {
  try {
    await waitFor(async () => findReplaceOpen(tabId), "", timeoutMs);
    return true;
  } catch (_error) {
    return false;
  }
}

async function activateFindReplaceMenuItem(tabId, keyboard = false) {
  await clickSelector(tabId, "#docs-edit-menu");
  let menuItem = null;
  let menuAXItem = null;
  await waitFor(
    async () => {
      menuItem = await evaluate(tabId, findReplaceMenuItemExpression());
      menuAXItem = menuItem ? null : await findReplaceAXMenuItem(tabId);
      return Boolean(menuItem || menuAXItem);
    },
    "The Find and replace menu item is unavailable.",
    2500,
  );
  if (!keyboard) {
    if (menuItem) {
      await clickPoint(tabId, menuItem);
      return;
    }
    if (await clickAXNode(tabId, menuAXItem)) return;
    throw new Error("The Find and replace menu item is unavailable.");
  }
  const focused = menuAXItem
    ? await focusAXNode(tabId, menuAXItem)
    : await evaluate(tabId, focusFindReplaceMenuItemExpression());
  if (!focused) {
    throw new Error("The Find and replace menu item is unavailable.");
  }
  await dispatchShortcut(tabId, "Enter", "Enter", 0);
}

async function openFindReplace(tabId) {
  if (await findReplaceOpen(tabId)) return;
  try {
    await activateFindReplaceMenuItem(tabId);
  } catch (_error) {
    // The documented shortcut below is the first fallback when menu discovery fails.
  }
  if (await waitForFindReplaceOpen(tabId, 4000)) return;
  await dispatchShortcut(tabId, "Escape", "Escape", 0);
  const platform = String(await evaluate(tabId, "navigator.platform || ''"));
  const modifiers = platform.toLowerCase().includes("mac") ? 12 : 2;
  await dispatchShortcut(tabId, "H", "KeyH", modifiers);
  if (await waitForFindReplaceOpen(tabId, 5000)) return;
  await dispatchShortcut(tabId, "Escape", "Escape", 0);
  try {
    await activateFindReplaceMenuItem(tabId, true);
  } catch (_error) {
    // The bounded final wait below produces content-free diagnostics.
  }
  if (await waitForFindReplaceOpen(tabId, 15000)) return;
  const diagnostics = await findReplaceAXDiagnostics(tabId);
  throw new Error(
    `Google Docs Find and replace did not open. ` +
    `(dialog=${diagnostics.dialog}; textbox=${diagnostics.textbox}; ` +
    `find=${diagnostics.find}; replace=${diagnostics.replace})`,
  );
}

async function focusFindReplaceAXInput(tabId, kind) {
  const controls = await findReplaceAXState(tabId);
  const node = kind === "find" ? controls.findInput : controls.replaceInput;
  return node ? focusAXNode(tabId, node) : false;
}

async function focusReplaceFromFindInput(tabId) {
  const controls = await findReplaceAXState(tabId);
  if (!controls.findInput || !controls.replaceInput) {
    return false;
  }
  await dispatchShortcut(tabId, "Tab", "Tab", 0);
  await sleep(200);
  const updated = await findReplaceAXState(tabId);
  if (updated.replaceInput && axFocused(updated.replaceInput)) return true;
  return Boolean(await evaluate(tabId, findReplaceContextExpression(`
    return document.activeElement === replaceInput;
  `)));
}

async function findReplaceAXInputEquals(tabId, kind, expected) {
  const controls = await findReplaceAXState(tabId);
  const node = kind === "find" ? controls.findInput : controls.replaceInput;
  return Boolean(node && String(axValue(node, "value")) === expected);
}

async function fillFindReplaceInput(tabId, kind, text) {
  const focused = kind === "replace" && await focusReplaceFromFindInput(tabId)
    ? true
    : await focusFindReplaceAXInput(tabId, kind);
  if (!focused) {
    const field = kind === "find" ? "findInput" : "replaceInput";
    const focused = await evaluate(tabId, findReplaceContextExpression(`
      const element = ${field};
      element.focus();
      return true;
    `));
    if (!focused) {
      throw new Error("A Google Docs Find and replace input is unavailable.");
    }
  }
  await sleep(150);
  const platform = String(await evaluate(tabId, "navigator.platform || ''"));
  const modifiers = platform.toLowerCase().includes("mac") ? 4 : 2;
  await dispatchShortcut(tabId, "a", "KeyA", modifiers);
  await command(tabId, "Input.insertText", { text });
  const field = kind === "find" ? "findInput" : "replaceInput";
  const accepted = async () => {
    if (await findReplaceAXInputEquals(tabId, kind, text)) return true;
    return Boolean(await evaluate(tabId, findReplaceContextExpression(`
      const element = ${field};
      return (typeof element.value === "string" ? element.value : element.textContent) === ${JSON.stringify(text)};
    `)));
  };
  try {
    await waitFor(accepted, "", 3000);
    return;
  } catch (_error) {
    // Retry after a fresh semantic focus; Docs can move focus while opening the dialog.
  }
  if (!await focusFindReplaceAXInput(tabId, kind)) {
    throw new Error(`Google Docs ${kind} input is unavailable.`);
  }
  await dispatchShortcut(tabId, "a", "KeyA", modifiers);
  await command(tabId, "Input.insertText", { text });
  if (await waitForFindReplaceField(accepted, 5000)) return;
  const setThroughDialog = await evaluate(tabId, findReplaceContextExpression(`
    const element = ${field};
    const prototype = element instanceof HTMLInputElement
      ? HTMLInputElement.prototype
      : element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : null;
    if (!prototype) return false;
    const descriptor = Object.getOwnPropertyDescriptor(prototype, "value");
    if (!descriptor || typeof descriptor.set !== "function") return false;
    descriptor.set.call(element, ${JSON.stringify(text)});
    element.dispatchEvent(new InputEvent("input", {
      bubbles: true,
      composed: true,
      data: ${JSON.stringify(text)},
      inputType: "insertText",
    }));
    element.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
    return element.value === ${JSON.stringify(text)};
  `));
  if (setThroughDialog && await waitForFindReplaceField(accepted, 3000)) return;
  throw new Error(`Google Docs did not accept the exact ${kind} field.`);
}

async function waitForFindReplaceField(check, timeoutMs) {
  try {
    await waitFor(check, "", timeoutMs);
    return true;
  } catch (_error) {
    return false;
  }
}

async function findReplaceAXCheckbox(tabId, exactName) {
  return namedAXNode(await accessibilityNodes(tabId), "checkbox", exactName);
}

async function findReplaceAXNamedControl(tabId, role, exactName) {
  return namedAXNode(await accessibilityNodes(tabId), role, exactName);
}

async function checkboxState(tabId, exactName) {
  const axNode = await findReplaceAXCheckbox(tabId, exactName);
  if (axNode) return axChecked(axNode);
  return evaluate(tabId, findReplaceContextExpression(`
    const expected = ${JSON.stringify(exactName.toLowerCase())};
    const candidates = Array.from(root.querySelectorAll('[role="checkbox"],input[type="checkbox"]'));
    const element = candidates.find((candidate) => {
      const names = [
        candidate.getAttribute("aria-label"), candidate.textContent,
        candidate.parentElement?.innerText, candidate.parentElement?.parentElement?.innerText,
      ].map(normalize);
      return names.some((name) => name === expected);
    });
    if (!element) return null;
    if (typeof element.checked === "boolean") return element.checked;
    return element.getAttribute("aria-checked") === "true" || /checked/.test(element.className || "");
  `));
}

async function clickFindReplaceControl(tabId, role, exactName) {
  const axNode = await findReplaceAXNamedControl(tabId, role, exactName);
  if (axNode && await clickAXNode(tabId, axNode)) return;
  const point = await evaluate(tabId, findReplaceContextExpression(`
    const expected = ${JSON.stringify(exactName.toLowerCase())};
    const selector = ${JSON.stringify(`[role="${role}"],${role === "button" ? "button" : "input[type=checkbox]"}`)};
    const candidates = Array.from(root.querySelectorAll(selector));
    const element = candidates.find((candidate) => {
      if (!visible(candidate)) return false;
      const names = [
        candidate.getAttribute("aria-label"), candidate.getAttribute("data-tooltip"), candidate.textContent,
        candidate.parentElement?.innerText, candidate.parentElement?.parentElement?.innerText,
      ].map(normalize);
      return names.some((name) => name === expected);
    });
    if (!element) return null;
    const rect = element.getBoundingClientRect();
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
  `));
  if (!point) {
    throw new Error(`Google Docs ${exactName} control is unavailable.`);
  }
  await clickPoint(tabId, point);
}

async function findReplaceHasUniqueMatch(tabId) {
  if (await findReplaceAXHasUniqueMatch(tabId)) return true;
  return Boolean(await evaluate(tabId, findReplaceContextExpression(
    'return /\\b1\\s+of\\s+1\\b/i.test(root.innerText || root.textContent || "");',
  )));
}

async function configureFindOptions(tabId) {
  const matchCase = await checkboxState(tabId, "Match case");
  if (matchCase === null) {
    throw new Error("Google Docs Match case control is unavailable.");
  }
  if (!matchCase) {
    await clickFindReplaceControl(tabId, "checkbox", "Match case");
  }
  for (const name of ["Match using regular expressions", "Ignore Latin diacritics"]) {
    const state = await checkboxState(tabId, name);
    if (state === true) {
      await clickFindReplaceControl(tabId, "checkbox", name);
    }
  }
}

async function replaceUnique(tabId, documentId, edit, jobId, firstMutation) {
  await openFindReplace(tabId);
  await configureFindOptions(tabId);
  await fillFindReplaceInput(tabId, "find", edit.find);
  await fillFindReplaceInput(tabId, "replace", edit.replace);
  await waitFor(
    async () => findReplaceHasUniqueMatch(tabId),
    "Google Docs did not confirm exactly one live match.",
    8000,
  );
  if (firstMutation) {
    await assertFocusedDocumentTab(tabId, documentId);
    const boundary = await requestMutationBoundary(jobId);
    if (boundary !== true) {
      throw new Error("The local adapter did not authorize the mutation boundary.");
    }
  }
  await clickFindReplaceControl(tabId, "button", "Replace");
  await sleep(450);
  await dispatchShortcut(tabId, "Escape", "Escape", 0);
}

async function navigateToTab(tabId, documentId, documentTabId) {
  const url = new URL(`https://docs.google.com/document/d/${encodeURIComponent(documentId)}/edit`);
  if (documentTabId) {
    url.searchParams.set("tab", documentTabId);
  }
  await command(tabId, "Page.navigate", { url: url.toString() });
  await waitFor(
    async () => Boolean(await evaluate(tabId, `(() => {
      const url = new URL(location.href);
      const match = url.pathname.match(/^\\/document\\/d\\/([^/]+)\\//);
      if (!match || decodeURIComponent(match[1]) !== ${JSON.stringify(documentId)}) return false;
      return (url.searchParams.get("tab") || "") === ${JSON.stringify(documentTabId)};
    })()`)),
    "The active Google Docs tab did not navigate to the approved document tab.",
    30000,
  );
  await waitForEditor(tabId);
}

async function executeJob(job, tab) {
  const tabId = tab.id;
  let attached = false;
  let mutationStarted = false;
  let completed = 0;
  let modeVerified = false;
  try {
    await assertFocusedDocumentTab(tabId, job.document_id);
    await chrome.debugger.attach(debuggee(tabId), "1.3");
    attached = true;
    await command(tabId, "Runtime.enable");
    await command(tabId, "Page.enable");
    await command(tabId, "DOM.enable");
    await command(tabId, "Accessibility.enable");
    let currentTab = documentTabIdFromUrl(tab.url || "");
    await waitForEditor(tabId);
    for (const edit of job.edits) {
      if (edit.tab_id !== currentTab) {
        await navigateToTab(tabId, job.document_id, edit.tab_id);
        currentTab = edit.tab_id;
      }
      await ensureSuggesting(tabId);
      modeVerified = true;
      await replaceUnique(tabId, job.document_id, edit, job.job_id, !mutationStarted);
      mutationStarted = true;
      completed += 1;
      await ensureSuggesting(tabId);
    }
    await sleep(1500);
    return {
      job_id: job.job_id,
      status: "ok",
      mode_verified: modeVerified,
      edit_count: completed,
      mutation_started: mutationStarted,
    };
  } catch (error) {
    return {
      job_id: job.job_id,
      status: "error",
      mode_verified: modeVerified,
      edit_count: completed,
      mutation_started: mutationStarted,
      error: error instanceof Error ? error.message.slice(0, 500) : "Extension operation failed.",
    };
  } finally {
    if (attached) {
      try {
        await command(tabId, "Accessibility.disable");
        await command(tabId, "DOM.disable");
      } catch (_error) {
        // The page may already be gone; detach still releases the debugger.
      }
      try {
        await chrome.debugger.detach(debuggee(tabId));
      } catch (_error) {
        // The tab may have closed. API read-back remains authoritative.
      }
    }
  }
}

async function handleNativeMessage(message) {
  if (!message || message.protocol !== BRIDGE_PROTOCOL) {
    throw new Error("The local native connector protocol does not match this extension.");
  }
  if (message.type === "ready") {
    await setConnectorState("connected");
    return;
  }
  if (message.type === "mutation-authorized") {
    if (!pendingBoundary || message.job_id !== pendingBoundary.jobId) {
      throw new Error("The local adapter returned an unexpected mutation authorization.");
    }
    if (message.authorized === true) {
      pendingBoundary.resolve(true);
    } else {
      pendingBoundary.reject(new Error("The governed mutation boundary rejected the edit."));
    }
    return;
  }
  if (message.type !== "job") {
    throw new Error("The local native connector returned an unknown message.");
  }
  if (activeJob) {
    throw new Error("Another Google Docs edit is already active.");
  }
  const job = validateJob(message.job);
  activeJob = job;
  await setConnectorState("working");
  const tab = await activateDocumentTab(job.document_id);
  const result = await executeJob(job, tab);
  nativeSend({ type: "result", result });
  activeJob = null;
  if (result.status === "ok") {
    await setConnectorState("connected");
  } else {
    await setConnectorState("error", result.error || "Extension operation failed.");
  }
}

async function handleMessage(message) {
  if (!message || message.type !== "get-status") {
    throw new Error("Unknown extension request.");
  }
  const stored = await chrome.storage.session.get(CONNECTOR_STATE_KEY);
  return stored[CONNECTOR_STATE_KEY] || { state: nativePort ? "connected" : "offline", detail: "" };
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  handleMessage(message)
    .then((value) => sendResponse({ ok: true, value }))
    .catch((error) => sendResponse({
      ok: false,
      error: error instanceof Error ? error.message : "Extension request failed.",
    }));
  return true;
});

chrome.windows.onFocusChanged.addListener((windowId) => {
  rememberNormalWindow(windowId).catch(() => {});
});

connectNativeBridge();
