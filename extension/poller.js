function requestAutomaticCheck() {
  chrome.runtime.sendMessage({ type: "auto-poll" }).catch(() => {
    // The service worker can be restarting during an extension update.
  });
}

requestAutomaticCheck();
setInterval(requestAutomaticCheck, 3000);
