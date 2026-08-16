const pairSection = document.getElementById("pair-section");
const jobSection = document.getElementById("job-section");
const jobDetails = document.getElementById("job");
const status = document.getElementById("status");
const port = document.getElementById("port");
const pairingCode = document.getElementById("pairing-code");
const pairButton = document.getElementById("pair");
const checkButton = document.getElementById("check");
const applyButton = document.getElementById("apply");
const planHash = document.getElementById("plan-hash");
const editCount = document.getElementById("edit-count");

let pendingPlanHash = "";

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
    showStatus("Paired. Start an approved apply command, then check for the job.", "success");
  } catch (error) {
    showStatus(error.message, "error");
  } finally {
    setBusy(pairButton, false);
  }
});

checkButton.addEventListener("click", async () => {
  setBusy(checkButton, true);
  jobDetails.hidden = true;
  pendingPlanHash = "";
  showStatus("Checking the local adapter…");
  try {
    const job = await request({ type: "check-job" });
    pendingPlanHash = job.planSha256;
    planHash.textContent = `${job.planSha256.slice(0, 12)}…${job.planSha256.slice(-8)}`;
    planHash.title = job.planSha256;
    editCount.textContent = String(job.editCount);
    jobDetails.hidden = false;
    showStatus("Approved job found. Review the hash, then apply.", "success");
  } catch (error) {
    showStatus(error.message, "error");
  } finally {
    setBusy(checkButton, false);
  }
});

applyButton.addEventListener("click", async () => {
  if (!pendingPlanHash) {
    showStatus("Check for the approved job again.", "error");
    return;
  }
  setBusy(applyButton, true);
  checkButton.disabled = true;
  showStatus("Applying in Suggesting mode and waiting for verification…");
  try {
    const result = await request({ type: "apply-job", planSha256: pendingPlanHash });
    jobDetails.hidden = true;
    pendingPlanHash = "";
    showStatus(`${result.editCount} tracked suggestion replacement(s) sent for API verification.`, "success");
  } catch (error) {
    showStatus(error.message, "error");
  } finally {
    setBusy(applyButton, false);
    checkButton.disabled = false;
  }
});

initialize();
