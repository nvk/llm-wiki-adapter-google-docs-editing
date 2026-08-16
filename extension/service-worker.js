const BRIDGE_PROTOCOL = "llm-wiki-google-docs-extension/v1";
const DEFAULT_PORT = 17843;

chrome.runtime.onInstalled.addListener(async () => {
  await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
});

chrome.runtime.onStartup.addListener(async () => {
  await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
});

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

async function activeDocumentTab(documentId) {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  const tab = tabs[0];
  if (!tab || typeof tab.id !== "number" || documentIdFromUrl(tab.url || "") !== documentId) {
    throw new Error("Open the exact approved Google Doc in the active tab before applying.");
  }
  return tab;
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

async function waitForEditor(tabId) {
  await waitFor(
    async () => Boolean(await evaluate(tabId, `Boolean(document.querySelector("#docs-editor") && document.querySelector("#docs-mode-switcher-select"))`)),
    "The Google Docs editor did not become ready.",
    30000,
  );
}

async function modeText(tabId) {
  return String(await evaluate(tabId, `(() => {
    const element = document.querySelector("#docs-mode-switcher-select");
    if (!element) return "";
    return [element.getAttribute("aria-label"), element.getAttribute("data-tooltip"), element.getAttribute("title"), element.textContent]
      .filter(Boolean).join(" ").toLowerCase();
  })()`));
}

async function ensureSuggesting(tabId) {
  if (!(await modeText(tabId)).includes("suggest")) {
    await clickSelector(tabId, "#docs-mode-switcher-select");
    await waitFor(
      async () => Boolean(await evaluate(tabId, visibleElementExpression("#docs-mode-switcher-suggesting"))),
      "Suggesting mode is not available for this account or document.",
    );
    await clickSelector(tabId, "#docs-mode-switcher-suggesting");
  }
  await waitFor(
    async () => (await modeText(tabId)).includes("suggest"),
    "Google Docs did not confirm Suggesting mode.",
  );
}

async function openFindReplace(tabId) {
  await clickSelector(tabId, "#docs-edit-menu");
  try {
    await waitFor(
      async () => Boolean(await evaluate(tabId, visibleElementExpression("#docs-find-and-replace"))),
      "",
      2500,
    );
    await clickSelector(tabId, "#docs-find-and-replace");
  } catch (_error) {
    await command(tabId, "Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape" });
    await command(tabId, "Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape" });
    const platform = String(await evaluate(tabId, "navigator.platform || ''"));
    const modifiers = platform.toLowerCase().includes("mac") ? 12 : 10;
    await command(tabId, "Input.dispatchKeyEvent", {
      type: "keyDown", key: "H", code: "KeyH", modifiers,
    });
    await command(tabId, "Input.dispatchKeyEvent", {
      type: "keyUp", key: "H", code: "KeyH", modifiers,
    });
  }
  await waitFor(
    async () => Boolean(await evaluate(tabId, visibleElementExpression(".docs-findandreplacedialog"))),
    "Google Docs Find and replace did not open.",
  );
}

async function fillInput(tabId, selector, text) {
  const focused = await evaluate(tabId, `(() => {
    const element = document.querySelector(${JSON.stringify(selector)});
    if (!element) return false;
    element.focus();
    return true;
  })()`);
  if (!focused) {
    throw new Error("A Google Docs Find and replace input is unavailable.");
  }
  const platform = String(await evaluate(tabId, "navigator.platform || ''"));
  const modifiers = platform.toLowerCase().includes("mac") ? 4 : 2;
  await command(tabId, "Input.dispatchKeyEvent", {
    type: "keyDown", key: "a", code: "KeyA", modifiers,
  });
  await command(tabId, "Input.dispatchKeyEvent", {
    type: "keyUp", key: "a", code: "KeyA", modifiers,
  });
  await command(tabId, "Input.insertText", { text });
  const accepted = await evaluate(tabId, `document.querySelector(${JSON.stringify(selector)})?.value === ${JSON.stringify(text)}`);
  if (!accepted) {
    throw new Error("Google Docs did not accept an exact replacement field.");
  }
}

async function checkboxState(tabId, exactName) {
  return evaluate(tabId, `(() => {
    const root = document.querySelector(".docs-findandreplacedialog");
    if (!root) return null;
    const expected = ${JSON.stringify(exactName.toLowerCase())};
    const candidates = Array.from(root.querySelectorAll('[role="checkbox"],input[type="checkbox"]'));
    const element = candidates.find((candidate) => {
      const name = [candidate.getAttribute("aria-label"), candidate.textContent].filter(Boolean).join(" ").trim().toLowerCase();
      const parentName = candidate.parentElement ? candidate.parentElement.innerText.trim().toLowerCase() : "";
      return name === expected || parentName === expected;
    });
    if (!element) return null;
    if (typeof element.checked === "boolean") return element.checked;
    return element.getAttribute("aria-checked") === "true" || /checked/.test(element.className || "");
  })()`);
}

async function configureFindOptions(tabId) {
  const matchCase = await checkboxState(tabId, "Match case");
  if (matchCase === null) {
    throw new Error("Google Docs Match case control is unavailable.");
  }
  if (!matchCase) {
    await clickNamedControl(tabId, "checkbox", "Match case", ".docs-findandreplacedialog");
  }
  for (const name of ["Match using regular expressions", "Ignore Latin diacritics"]) {
    const state = await checkboxState(tabId, name);
    if (state === true) {
      await clickNamedControl(tabId, "checkbox", name, ".docs-findandreplacedialog");
    }
  }
}

async function replaceUnique(tabId, edit, jobId, firstMutation) {
  await openFindReplace(tabId);
  await configureFindOptions(tabId);
  await fillInput(tabId, "input.docs-findandreplacedialog-find-input", edit.find);
  await fillInput(tabId, "input.docs-findandreplacedialog-replace-input", edit.replace);
  await waitFor(
    async () => Boolean(await evaluate(tabId, `(() => {
      const dialog = document.querySelector(".docs-findandreplacedialog");
      return dialog ? /\\b1\\s+of\\s+1\\b/i.test(dialog.innerText || dialog.textContent || "") : false;
    })()`)),
    "Google Docs did not confirm exactly one live match.",
    8000,
  );
  if (firstMutation) {
    const boundary = await bridgeFetch("/v1/before-mutation", {
      method: "POST",
      body: { job_id: jobId },
    });
    if (boundary.mutation_authorized !== true) {
      throw new Error("The local adapter did not authorize the mutation boundary.");
    }
  }
  await clickNamedControl(tabId, "button", "Replace", ".docs-findandreplacedialog");
  await sleep(450);
  await command(tabId, "Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape" });
  await command(tabId, "Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape" });
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

async function executeJob(job) {
  const tab = await activeDocumentTab(job.document_id);
  const tabId = tab.id;
  let attached = false;
  let mutationStarted = false;
  let completed = 0;
  let modeVerified = false;
  try {
    await chrome.debugger.attach(debuggee(tabId), "1.3");
    attached = true;
    await command(tabId, "Runtime.enable");
    await command(tabId, "Page.enable");
    let currentTab = null;
    for (const edit of job.edits) {
      if (edit.tab_id !== currentTab) {
        await navigateToTab(tabId, job.document_id, edit.tab_id);
        currentTab = edit.tab_id;
      }
      await ensureSuggesting(tabId);
      modeVerified = true;
      await replaceUnique(tabId, edit, job.job_id, !mutationStarted);
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

async function applyCurrentJob(expectedPlanHash) {
  const job = await getJob();
  if (job.plan_sha256 !== expectedPlanHash) {
    throw new Error("The pending approved-plan hash changed; check the job again.");
  }
  const result = await executeJob(job);
  await bridgeFetch("/v1/result", { method: "POST", body: result });
  if (result.status !== "ok") {
    throw new Error(result.error || "Google Docs suggestion operation failed.");
  }
  return { applied: true, editCount: result.edit_count, planSha256: job.plan_sha256 };
}

async function handleMessage(message) {
  switch (message && message.type) {
    case "get-config": {
      const config = await configuration();
      return { paired: Boolean(config.token), port: config.port };
    }
    case "pair":
      return pair(message);
    case "check-job": {
      const job = await getJob();
      return { available: true, planSha256: job.plan_sha256, editCount: job.edits.length };
    }
    case "apply-job":
      return applyCurrentJob(String(message.planSha256 || ""));
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
