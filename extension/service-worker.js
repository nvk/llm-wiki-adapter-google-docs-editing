const BRIDGE_PROTOCOL = "llm-wiki-google-docs-extension/v1";
const DEFAULT_PORT = 17843;
const POLL_ALARM = "approved-edit-poll";
let automaticRun = null;

async function configureExtension() {
  await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
  const alarm = await chrome.alarms.get(POLL_ALARM);
  if (!alarm) {
    await chrome.alarms.create(POLL_ALARM, { periodInMinutes: 0.5 });
  }
}

chrome.runtime.onInstalled.addListener(configureExtension);
chrome.runtime.onStartup.addListener(configureExtension);
configureExtension().catch(() => {});

function assertPort(value) {
  const port = Number(value);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error("Bridge port must be between 1 and 65535.");
  }
  return port;
}

async function configuration() {
  const stored = await chrome.storage.local.get(["bridgePort", "bridgeToken"]);
  return {
    port: assertPort(stored.bridgePort || DEFAULT_PORT),
    token: typeof stored.bridgeToken === "string" ? stored.bridgeToken : "",
  };
}

async function bridgeFetch(path, { method = "GET", body = null, authenticated = true } = {}) {
  const config = await configuration();
  if (authenticated && !config.token) {
    throw new Error("Pair this extension with the local adapter first.");
  }
  const headers = { "Content-Type": "application/json" };
  if (authenticated) {
    headers.Authorization = `Bearer ${config.token}`;
  }
  const response = await fetch(`http://127.0.0.1:${config.port}${path}`, {
    method,
    headers,
    body: body === null ? null : JSON.stringify(body),
    cache: "no-store",
  });
  let value;
  try {
    value = await response.json();
  } catch (_error) {
    throw new Error("The local adapter returned an invalid response.");
  }
  if (!response.ok) {
    throw new Error(typeof value.error === "string" ? value.error : "The local adapter rejected the request.");
  }
  if (value.protocol !== BRIDGE_PROTOCOL) {
    throw new Error("The local adapter bridge protocol does not match this extension.");
  }
  return value;
}

async function pair(message) {
  const port = assertPort(message.port || DEFAULT_PORT);
  const pairingCode = String(message.pairingCode || "").trim();
  if (!/^\d{8}$/.test(pairingCode)) {
    throw new Error("Enter the eight-digit pairing code printed by the adapter.");
  }
  await chrome.storage.local.set({ bridgePort: port });
  const result = await bridgeFetch("/v1/pair", {
    method: "POST",
    body: { pairing_code: pairingCode },
    authenticated: false,
  });
  if (result.paired !== true || typeof result.token !== "string" || result.token.length < 32) {
    throw new Error("The local adapter did not return a valid pairing token.");
  }
  await chrome.storage.local.set({ bridgeToken: result.token });
  return { paired: true, port };
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

async function getJob() {
  return validateJob(await bridgeFetch("/v1/job"));
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

async function focusedDocumentTab(documentId) {
  const [tabs, focusedWindow] = await Promise.all([
    chrome.tabs.query({ url: "https://docs.google.com/document/*" }),
    chrome.windows.getLastFocused({ windowTypes: ["normal"] }),
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

async function assertFocusedDocumentTab(tabId, documentId) {
  const tab = await focusedDocumentTab(documentId);
  if (!tab || tab.id !== tabId) {
    throw new Error(
      "Keep the approved Google Doc active in the most recently focused normal Chrome window.",
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
    const dialogs = Array.from(document.querySelectorAll('[role="dialog"],.docs-dialog,.modal-dialog'))
      .filter(visible);
    const namedDialog = dialogs.find((candidate) => {
      const name = normalize([candidate.getAttribute("aria-label"), candidate.getAttribute("data-dialog-title")]
        .filter(Boolean).join(" "));
      const heading = Array.from(candidate.querySelectorAll('[role="heading"],h1,h2,h3,.docs-dialog-title'))
        .map((candidateHeading) => normalize(candidateHeading.textContent)).join(" ");
      return /find( and| &) replace/.test(name) || /find( and| &) replace/.test(heading);
    }) || null;
    const inputs = Array.from(document.querySelectorAll('input,[role="textbox"]')).filter(visible);
    const score = (element, kind) => {
      const name = label(element);
      const className = normalize(element.className);
      if (kind === "find") {
        return (/find-input/.test(className) ? 100 : 0) + (name === "find" ? 50 : 0);
      }
      return (/replace-input/.test(className) ? 100 : 0) + (/^replace( with)?$/.test(name) ? 50 : 0);
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

async function findReplaceOpen(tabId) {
  return Boolean(await evaluate(tabId, findReplaceContextExpression("return true;")));
}

async function dispatchShortcut(tabId, key, code, modifiers) {
  await command(tabId, "Input.dispatchKeyEvent", {
    type: "rawKeyDown", key, code, modifiers,
  });
  await command(tabId, "Input.dispatchKeyEvent", {
    type: "keyUp", key, code, modifiers,
  });
}

async function openFindReplace(tabId) {
  if (await findReplaceOpen(tabId)) return;
  await clickSelector(tabId, "#docs-edit-menu");
  try {
    let menuItem = null;
    await waitFor(
      async () => {
        menuItem = await evaluate(tabId, findReplaceMenuItemExpression());
        return Boolean(menuItem);
      },
      "",
      2500,
    );
    await clickPoint(tabId, menuItem);
  } catch (_error) {
    await dispatchShortcut(tabId, "Escape", "Escape", 0);
    const platform = String(await evaluate(tabId, "navigator.platform || ''"));
    const modifiers = platform.toLowerCase().includes("mac") ? 12 : 2;
    await dispatchShortcut(tabId, "H", "KeyH", modifiers);
  }
  await waitFor(
    async () => findReplaceOpen(tabId),
    "Google Docs Find and replace did not open.",
  );
}

async function fillFindReplaceInput(tabId, kind, text) {
  const field = kind === "find" ? "findInput" : "replaceInput";
  const focused = await evaluate(tabId, findReplaceContextExpression(`
    const element = ${field};
    element.focus();
    return true;
  `));
  if (!focused) {
    throw new Error("A Google Docs Find and replace input is unavailable.");
  }
  const platform = String(await evaluate(tabId, "navigator.platform || ''"));
  const modifiers = platform.toLowerCase().includes("mac") ? 4 : 2;
  await dispatchShortcut(tabId, "a", "KeyA", modifiers);
  await command(tabId, "Input.insertText", { text });
  const accepted = await evaluate(tabId, findReplaceContextExpression(`
    const element = ${field};
    return (typeof element.value === "string" ? element.value : element.textContent) === ${JSON.stringify(text)};
  `));
  if (!accepted) {
    throw new Error("Google Docs did not accept an exact replacement field.");
  }
}

async function checkboxState(tabId, exactName) {
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
    async () => Boolean(await evaluate(tabId, findReplaceContextExpression(
      'return /\\b1\\s+of\\s+1\\b/i.test(root.innerText || root.textContent || "");',
    ))),
    "Google Docs did not confirm exactly one live match.",
    8000,
  );
  if (firstMutation) {
    await assertFocusedDocumentTab(tabId, documentId);
    const boundary = await bridgeFetch("/v1/before-mutation", {
      method: "POST",
      body: { job_id: jobId },
    });
    if (boundary.mutation_authorized !== true) {
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
        await chrome.debugger.detach(debuggee(tabId));
      } catch (_error) {
        // The tab may have closed. API read-back remains authoritative.
      }
    }
  }
}

async function attemptAutomaticJob() {
  if (automaticRun) {
    return automaticRun;
  }
  automaticRun = (async () => {
    let job;
    try {
      job = await getJob();
    } catch (_error) {
      return { state: "idle" };
    }
    const tab = await focusedDocumentTab(job.document_id);
    if (!tab) {
      return { state: "waiting-for-document" };
    }
    const result = await executeJob(job, tab);
    await bridgeFetch("/v1/result", { method: "POST", body: result });
    return { state: result.status, editCount: result.edit_count };
  })();
  try {
    return await automaticRun;
  } finally {
    automaticRun = null;
  }
}

async function handleMessage(message) {
  switch (message && message.type) {
    case "get-config": {
      const config = await configuration();
      return { paired: Boolean(config.token), port: config.port };
    }
    case "pair":
      return pair(message);
    case "auto-poll":
      return attemptAutomaticJob();
    default:
      throw new Error("Unknown extension request.");
  }
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

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === POLL_ALARM) {
    attemptAutomaticJob().catch(() => {});
  }
});

setTimeout(() => attemptAutomaticJob().catch(() => {}), 1000);
