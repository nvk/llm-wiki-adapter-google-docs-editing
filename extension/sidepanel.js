const pairSection = document.getElementById("pair-section");
const jobSection = document.getElementById("job-section");
const status = document.getElementById("status");
const port = document.getElementById("port");
const pairingCode = document.getElementById("pairing-code");
const pairButton = document.getElementById("pair");

async function request(message) {
  const response = await chrome.runtime.sendMessage(message);
  if (!response || response.ok !== true) {
    throw new Error(response && response.error ? response.error : "Extension service worker did not respond.");
  }
  return response.value;
}

function showStatus(message, kind = "") {
  status.textContent = message;
  status.className = kind;
}

function setBusy(button, busy) {
  button.disabled = busy;
  button.setAttribute("aria-busy", String(busy));
}

async function initialize() {
  try {
    const config = await request({ type: "get-config" });
    port.value = String(config.port);
    pairSection.hidden = config.paired;
    jobSection.hidden = !config.paired;
    if (config.paired) {
      showStatus("Waiting automatically for an approved llm-wiki edit.", "success");
    }
  } catch (error) {
    showStatus(error.message, "error");
  }
}

pairButton.addEventListener("click", async () => {
  setBusy(pairButton, true);
  showStatus("Pairing…");
  try {
    await request({
      type: "pair",
      port: Number(port.value),
      pairingCode: pairingCode.value,
    });
    pairingCode.value = "";
    pairSection.hidden = true;
    jobSection.hidden = false;
    showStatus("Paired. Approved llm-wiki edits will run automatically.", "success");
  } catch (error) {
    showStatus(error.message, "error");
  } finally {
    setBusy(pairButton, false);
  }
});

initialize();
