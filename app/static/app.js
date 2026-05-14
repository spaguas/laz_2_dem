const uploadForm = document.getElementById("uploadForm");
const jobStatus = document.getElementById("jobStatus");
const jobIdEl = document.getElementById("jobId");
const jobStateEl = document.getElementById("jobState");
const jobMessageEl = document.getElementById("jobMessage");
const progressBarEl = document.getElementById("progressBar");
const progressTextEl = document.getElementById("progressText");
const downloadLinkEl = document.getElementById("downloadLink");
const jobSrsEl = document.getElementById("jobSrs");

let currentJobId = null;
let pollTimer = null;

function setJobView(data) {
  jobStatus.classList.remove("hidden");
  jobIdEl.textContent = `Job: ${data.id}`;
  jobStateEl.textContent = data.status;
  jobStateEl.className = `badge ${data.status}`;
  jobMessageEl.textContent = data.message;
  jobSrsEl.textContent = `Origem: ${data.source_srs || "-"} | Destino: ${data.target_srs || "-"}`;
  progressBarEl.style.width = `${data.progress}%`;
  progressTextEl.textContent = `${data.progress}%`;

  if (data.download_url) {
    downloadLinkEl.href = data.download_url;
    downloadLinkEl.classList.remove("hidden");
  } else {
    downloadLinkEl.classList.add("hidden");
  }
}

async function pollJob(jobId) {
  try {
    const response = await fetch(`/api/jobs/${jobId}`);
    if (!response.ok) {
      throw new Error("Falha ao consultar status do job.");
    }

    const data = await response.json();
    setJobView(data);

    if (data.status === "completed" || data.status === "failed") {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  } catch (error) {
    clearInterval(pollTimer);
    pollTimer = null;
    jobMessageEl.textContent = error.message;
  }
}

uploadForm.addEventListener("submit", async (event) => {
  event.preventDefault();

  const fileInput = document.getElementById("lazFile");
  const sourceSrsInput = document.getElementById("sourceSrs");
  const targetSrsInput = document.getElementById("targetSrs");
  if (!fileInput.files || fileInput.files.length === 0) {
    return;
  }

  const formData = new FormData(uploadForm);

  jobStatus.classList.remove("hidden");
  jobStateEl.textContent = "queued";
  jobStateEl.className = "badge queued";
  jobMessageEl.textContent = "Enviando arquivo...";
  jobSrsEl.textContent = `Origem: ${sourceSrsInput.value} | Destino: ${targetSrsInput.value}`;
  progressBarEl.style.width = "0%";
  progressTextEl.textContent = "0%";
  downloadLinkEl.classList.add("hidden");

  try {
    const response = await fetch("/api/jobs", {
      method: "POST",
      body: formData,
    });

    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || "Falha ao enviar arquivo.");
    }

    currentJobId = data.job_id;
    jobIdEl.textContent = `Job: ${currentJobId}`;
    jobMessageEl.textContent = data.message;

    if (pollTimer) {
      clearInterval(pollTimer);
    }

    pollTimer = setInterval(() => pollJob(currentJobId), 2000);
    await pollJob(currentJobId);
  } catch (error) {
    jobStateEl.textContent = "failed";
    jobStateEl.className = "badge failed";
    jobMessageEl.textContent = error.message;
  }
});
