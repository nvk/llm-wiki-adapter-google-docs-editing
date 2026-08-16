const title = document.getElementById("status-title");
const detail = document.getElementById("status-detail");
const dot = document.getElementById("status-dot");

function render(value) {
  const state = value && value.state ? value.state : "offline";
  const labels = {
    connected: "Connected to local adapter",
    connecting: "Connecting to local adapter…",
    working: "Applying approved suggestions…",
    error: "Connector needs attention",
    offline: "Local connector is offline",
  };
  title.textContent = labels[state] || labels.offline;
  detail.textContent = value && value.detail ? value.detail : (
    state === "offline"
      ? "Run adapter.py browser-install once, then reload this extension."
      : "No extension click is required for document edits."
  );
  dot.className = `dot ${state}`;
}

async function refresh() {
  try {
    const response = await chrome.runtime.sendMessage({ type: "get-status" });
    if (!response || response.ok !== true) throw new Error(response?.error || "Status unavailable.");
    render(response.value);
  } catch (error) {
    render({ state: "offline", detail: error instanceof Error ? error.message : "Status unavailable." });
  }
}

refresh();
setInterval(refresh, 1500);
