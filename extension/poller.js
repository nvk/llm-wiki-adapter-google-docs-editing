let pollingTimer = null;

function stopPolling() {
  if (pollingTimer !== null) {
    clearInterval(pollingTimer);
    pollingTimer = null;
  }
}

function requestAutomaticCheck() {
  try {
    if (!chrome.runtime || !chrome.runtime.id) {
      stopPolling();
      return;
    }
    chrome.runtime.sendMessage({ type: "auto-poll" }).catch(() => {
      // The service worker can be restarting during an extension update.
    });
  } catch (_error) {
    // Reloading an unpacked extension invalidates scripts already in open tabs.
    stopPolling();
  }
}

requestAutomaticCheck();
pollingTimer = setInterval(requestAutomaticCheck, 3000);
